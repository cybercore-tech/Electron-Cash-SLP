# Electrum - lightweight Bitcoin client
# Copyright (C) 2015 Thomas Voegtlin
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

# Wallet classes:
#   - ImportedAddressWallet: imported address, no keystore
#   - ImportedPrivkeyWallet: imported private keys, keystore
#   - Standard_Wallet: one keystore, P2PKH
#   - Multisig_Wallet: several keystores, P2SH


import copy
import errno
import json
import os
import queue
import random
import re
import time
import threading
from collections import defaultdict
from functools import partial

from .i18n import ngettext
from .util import NotEnoughFunds, NotEnoughFundsSlp, NotEnoughUnfrozenFundsSlp, ExcessiveFee, PrintError, UserCancelled, profiler, format_satoshis, format_time, finalization_print_error, is_verbose

from .address import Address, Script, ScriptOutput, PublicKey
from .bitcoin import *
from .version import *
from .keystore import load_keystore, Hardware_KeyStore, Imported_KeyStore, BIP32_KeyStore, xpubkey_to_address
from . import networks
from .storage import multisig_type

from . import transaction
from .transaction import Transaction, InputValueMissing
from .plugins import run_hook
from . import bitcoin
from . import coinchooser
from .synchronizer import Synchronizer
from .verifier import SPV
from . import schnorr
from . import ecc_fast

from . import paymentrequest
from .paymentrequest import PR_PAID, PR_UNPAID, PR_UNKNOWN, PR_EXPIRED
from .paymentrequest import InvoiceStore
from .contacts import Contacts

from .slp import SlpMessage, SlpParsingError, SlpUnsupportedSlpTokenType, SlpNoMintingBatonFound, OpreturnError
from . import slp_validator_0x01, slp_validator_0x01_nft1
from .slp_graph_search import slp_gs_mgr

def _(message): return message

TX_STATUS = [
    _('Unconfirmed parent'),
    _('Low fee'),
    _('Unconfirmed'),
    _('Not Verified'),
]

del _
from .i18n import _

DEFAULT_CONFIRMED_ONLY = False

def relayfee(network):
    RELAY_FEE = 5000
    MAX_RELAY_FEE = 50000
    f = network.relay_fee if network and network.relay_fee else RELAY_FEE
    return min(f, MAX_RELAY_FEE)

def dust_threshold(network):
    # Change < dust threshold is added to the tx fee
    #return 182 * 3 * relayfee(network) / 1000 # original Electrum logic
    #return 1 # <-- was this value until late Sept. 2018
    return 546 # hard-coded Bitcoin Cash dust threshold. Was changed to this as of Sept. 2018


def sweep_preparations(privkeys, network, imax=100):
    class InputsMaxxed(Exception):
        pass

    def append_utxos_to_inputs(inputs, pubkey, txin_type):
        if txin_type == 'p2pkh':
            address = Address.from_pubkey(pubkey)
        else:
            address = PublicKey.from_pubkey(pubkey)
        sh = address.to_scripthash_hex()
        u = network.synchronous_get(('blockchain.scripthash.listunspent', [sh]))
        for item in u:
            if len(inputs) >= imax:
                raise InputsMaxxed()
            item['address'] = address
            item['type'] = txin_type
            item['prevout_hash'] = item['tx_hash']
            item['prevout_n'] = item['tx_pos']
            item['pubkeys'] = [pubkey]
            item['x_pubkeys'] = [pubkey]
            item['signatures'] = [None]
            item['num_sig'] = 1
            inputs.append(item)

    def find_utxos_for_privkey(txin_type, privkey, compressed):
        pubkey = bitcoin.public_key_from_private_key(privkey, compressed)
        append_utxos_to_inputs(inputs, pubkey, txin_type)
        keypairs[pubkey] = privkey, compressed

    inputs = []
    keypairs = {}
    try:
        for sec in privkeys:
            txin_type, privkey, compressed = bitcoin.deserialize_privkey(sec)
            find_utxos_for_privkey(txin_type, privkey, compressed)
            # do other lookups to increase support coverage
            if is_minikey(sec):
                # minikeys don't have a compressed byte
                # we lookup both compressed and uncompressed pubkeys
                find_utxos_for_privkey(txin_type, privkey, not compressed)
            elif txin_type == 'p2pkh':
                # WIF serialization does not distinguish p2pkh and p2pk
                # we also search for pay-to-pubkey outputs
                find_utxos_for_privkey('p2pk', privkey, compressed)
            elif txin_type == 'p2sh':
                raise ValueError(_("The specified WIF key '{}' is a p2sh WIF key. These key types cannot be swept.").format(sec))
    except InputsMaxxed:
        pass
    if not inputs:
        raise ValueError(_('No inputs found. (Note that inputs need to be confirmed)'))
    return inputs, keypairs


def sweep(privkeys, network, config, recipient, fee=None, imax=100, sign_schnorr=False):
    inputs, keypairs = sweep_preparations(privkeys, network, imax)
    total = sum(i.get('value') for i in inputs)
    if fee is None:
        outputs = [(TYPE_ADDRESS, recipient, total)]
        tx = Transaction.from_io(inputs, outputs, sign_schnorr=sign_schnorr)
        fee = config.estimate_fee(tx.estimated_size())
    if total - fee < 0:
        raise NotEnoughFunds(_('Not enough funds on address.') + '\nTotal: %d satoshis\nFee: %d'%(total, fee))
    if total - fee < dust_threshold(network):
        raise NotEnoughFunds(_('Not enough funds on address.') + '\nTotal: %d satoshis\nFee: %d\nDust Threshold: %d'%(total, fee, dust_threshold(network)))

    outputs = [(TYPE_ADDRESS, recipient, total - fee)]
    locktime = network.get_local_height()

    tx = Transaction.from_io(inputs, outputs, locktime=locktime, sign_schnorr=sign_schnorr)
    tx.BIP_LI01_sort()
    tx.sign(keypairs)
    return tx


class Abstract_Wallet(PrintError):
    """
    Wallet classes are created to handle various address generation methods.
    Completion states (watching-only, single account, no seed, etc) are handled inside classes.
    """

    max_change_outputs = 3

    def __init__(self, storage):
        self.electrum_version = PACKAGE_VERSION
        self.storage = storage
        self.thread = None  # this is used by the qt main_window to store a QThread. We just make sure it's always defined as an attribute here.
        self.network = None
        # verifier (SPV) and synchronizer are started in start_threads
        self.synchronizer = None
        self.verifier = None

        # slp graph databases for token type 1 and NFT1
        self.slp_graph_0x01, self.slp_graph_0x01_nft = None, None

        # Removes defunct entries from self.pruned_txo asynchronously
        self.pruned_txo_cleaner_thread = None

        # Cache of Address -> (c,u,x) balance. This cache is used by
        # get_addr_balance to significantly speed it up (it is called a lot).
        # Cache entries are invalidated when tx's are seen involving this
        # address (address history chages). Entries to this cache are added
        # only inside get_addr_balance.
        # Note that this data structure is touched by the network and GUI
        # thread concurrently without the use of locks, because Python GIL
        # allows us to get away with such things. As such do not iterate over
        # this dict, but simply add/remove items to/from it in 1-liners (which
        # Python's GIL makes thread-safe implicitly).
        self._addr_bal_cache = {}

        # We keep a set of the wallet and receiving addresses so that is_mine()
        # checks are O(logN) rather than O(N). This creates/resets that cache.
        self.invalidate_address_set_cache()

        self.gap_limit_for_change = 20 # constant
        # saved fields
        self.use_change            = storage.get('use_change', True)
        self.multiple_change       = storage.get('multiple_change', False)
        self.labels                = storage.get('labels', {})
        # Frozen addresses
        frozen_addresses = storage.get('frozen_addresses',[])
        self.frozen_addresses = set(Address.from_string(addr)
                                    for addr in frozen_addresses)
        # Frozen coins (UTXOs) -- note that we have 2 independent levels of "freezing": address-level and coin-level.
        # The two types of freezing are flagged independently of each other and 'spendable' is defined as a coin that satisfies
        # BOTH levels of freezing.
        self.frozen_coins = set(storage.get('frozen_coins', []))
        # address -> list(txid, height)
        history = storage.get('addr_history',{})
        self._history = self.to_Address_dict(history)

        # there is a difference between wallet.up_to_date and interface.is_up_to_date()
        # interface.is_up_to_date() returns true when all requests have been answered and processed
        # wallet.up_to_date is true when the wallet is synchronized (stronger requirement)
        self.up_to_date = False

        # locks: if you need to take multiple ones, acquire them in the order they are defined here!
        self.lock = threading.RLock()

        # load requests
        requests = self.storage.get('payment_requests', {})
        for key, req in requests.items():
            req['address'] = Address.from_string(key)
        self.receive_requests = {req['address']: req
                                 for req in requests.values()}

        # Transactions pending verification.  A map from tx hash to transaction
        # height.  Access is contended so a lock is needed. Client code should
        # use get_unverified_tx to get a thread-safe copy of this dict.
        self.unverified_tx = defaultdict(int)

        # Verified transactions.  Each value is a (height, timestamp, block_pos) tuple.  Access with self.lock.
        self.verified_tx = storage.get('verified_tx3', {})

        # save wallet type the first time
        if self.storage.get('wallet_type') is None:
            self.storage.put('wallet_type', self.wallet_type)

        # invoices and contacts
        self.invoices = InvoiceStore(self.storage)
        self.contacts = Contacts(self.storage)

        # Now, finally, after object is constructed -- we can do this
        self.load_keystore()
        self.load_addresses()
        self.load_transactions()
        self.build_reverse_history()

        self.check_history()

        # Print debug message on finalization
        finalization_print_error(self, "[{}/{}] finalized".format(__class__.__name__, self.diagnostic_name()))

    @property
    def is_slp(self):
        ''' Note that the various Slp_* classes explicitly write to storage
        to set the proper wallet_type on construction unconditionally, so
        this should always be valid for SLP wallets. '''
        return "slp_" in self.storage.get('wallet_type', '')

    @classmethod
    def to_Address_dict(cls, d):
        '''Convert a dict of strings to a dict of Adddress objects.'''
        return {Address.from_string(text): value for text, value in d.items()}

    @classmethod
    def from_Address_dict(cls, d):
        '''Convert a dict of Address objects to a dict of strings.'''
        return {addr.to_storage_string(): value
                for addr, value in d.items()}

    def diagnostic_name(self):
        return self.basename()

    def __str__(self):
        return self.basename()

    def get_master_public_key(self):
        return None

    @profiler
    def load_transactions(self):
        txi = self.storage.get('txi', {})
        self.txi = {tx_hash: self.to_Address_dict(value)
                    for tx_hash, value in txi.items()
                    # skip empty entries to save memory and disk space
                    if value}
        txo = self.storage.get('txo', {})
        self.txo = {tx_hash: self.to_Address_dict(value)
                    for tx_hash, value in txo.items()
                    # skip empty entries to save memory and disk space
                    if value}
        self.tx_fees = self.storage.get('tx_fees', {})
        self.pruned_txo = self.storage.get('pruned_txo', {})
        self.pruned_txo_values = set(self.pruned_txo.values())
        tx_list = self.storage.get('transactions', {})

        self.transactions = {}
        for tx_hash, raw in tx_list.items():
            tx = Transaction(raw)
            self.transactions[tx_hash] = tx
            if not self.txi.get(tx_hash) and not self.txo.get(tx_hash) and (tx_hash not in self.pruned_txo_values):
                self.print_error("removing unreferenced tx", tx_hash)
                self.transactions.pop(tx_hash)

        self.slpv1_validity = self.storage.get('slpv1_validity', {})
        self.token_types = self.storage.get('token_types', {})
        self.tx_tokinfo = self.storage.get('tx_tokinfo', {})

        # load up slp_txo as defaultdict-of-defaultdict-of-dicts
        self._slp_txo = defaultdict(lambda: defaultdict(dict))
        for addr, addrdict in self.to_Address_dict(self.storage.get('slp_txo',{})).items():
            for txid, txdict in addrdict.items():
                # need to do this iteration since json stores int keys as decimal strings.
                self._slp_txo[addr][txid] = {int(idx):d for idx,d in txdict.items()}

        ok = self.storage.get('slp_data_version', False)
        if ok != 3:
            self.rebuild_slp()

    @profiler
    def save_transactions(self, write=False):
        with self.lock:
            tx = {}
            for k,v in self.transactions.items():
                tx[k] = str(v)
            self.storage.put('transactions', tx)
            txi = {tx_hash: self.from_Address_dict(value)
                   for tx_hash, value in self.txi.items()
                   # skip empty entries to save memory and disk space
                   if value}
            txo = {tx_hash: self.from_Address_dict(value)
                   for tx_hash, value in self.txo.items()
                   # skip empty entries to save memory and disk space
                   if value}
            self.storage.put('txi', txi)
            self.storage.put('txo', txo)
            self.storage.put('tx_fees', self.tx_fees)
            self.storage.put('pruned_txo', self.pruned_txo)
            history = self.from_Address_dict(self._history)
            self.storage.put('addr_history', history)

            ### SLP stuff
            self.storage.put('slpv1_validity', self.slpv1_validity)
            self.storage.put('token_types', self.token_types)
            self.storage.put('slp_txo', self.from_Address_dict(self._slp_txo))
            self.storage.put('tx_tokinfo', self.tx_tokinfo)

            self.storage.put('slp_data_version', 3)

            if write:
                self.storage.write()

    def activate_slp(self):
        # This gets called in two situations:
        # - Upon wallet startup, it checks config to see if SLP should be enabled.
        # - During wallet operation, on a network reconnect, to "wake up" the validator -- According to JSCramer this is required.  TODO: Investigate why that is
        with self.lock:
            for tx_hash, tti in self.tx_tokinfo.items():
                # Fire up validation on unvalidated txes
                try:
                    tx = self.transactions[tx_hash]
                    self.slp_check_validation(tx_hash, tx)
                except KeyError:
                    continue

    _add_token_hex_re = re.compile('^[a-f0-9]{64}$')
    def add_token_type(self, token_id, entry, check_validation=True):
        if not isinstance(token_id, str) or not self._add_token_hex_re.match(token_id):
            # Paranoia: we enforce canonical hex string as lowercase to avoid
            # problems with the same token-id being added as upper or lowercase
            # by client code.  This is because token_id becomes a dictionary key
            # in various places and it not being identical would create chaos.
            raise ValueError('token_id must be a lowercase hex string of exactly 64 characters!')
        with self.lock:
            self.token_types[token_id] = dict(entry)
            self.storage.put('token_types', self.token_types)
            for tx_hash, tti in self.tx_tokinfo.items():
                # Fire up validation on unvalidated txes of matching token_id
                try:
                    if tti['token_id'] == token_id and check_validation:
                        tx = self.transactions[tx_hash]
                        self.slp_check_validation(tx_hash, tx)
                except KeyError: # This catches the case where tx_tokinfo was set to {}
                    continue

    def add_token_safe(self, token_class: str, token_id: str, token_name: str,
                       decimals_divisibility: int,
                       *, error_callback=None, allow_overwrite=False,
                       write_storage=True) -> bool:
        ''' This code was refactored from main_window.py to allow other
        subsystems (eg CLI/RPC, other platforms, etc) to add tokens.
        This function does some minimal sanity checks and returns True
        on success or False on failure.  The optional error_callback
        is called on False return. The callback takes a single translated string
        argument which is an error message (suitable for display to the user).

        On success (True) return, this method ends up calling
        self.add_token_type(), and also will end up saving the changes to
        wallet storage if write_storage=True (the default).

        This function is thread-safe. '''

        token_name = token_name.strip()
        token_id = token_id.strip().lower()

        # Check for duplication error
        d = self.token_types.get(token_id)
        group_id = d.get('group_id', None) if d else None
        if d is not None and not allow_overwrite:
            if error_callback:
                error_callback(_('Token with this hash id already exists'))
            return False
        for tid, d in self.token_types.copy().items():  # <-- must take a snapshot-copy here since we aren't holding locks and other threads may modify this dict as we iterate
            if d['name'] == token_name and tid != token_id:
                token_name = token_name + "-" + token_id[:3]
                break

        #Hash id validation
        gothex = self._add_token_hex_re.match(token_id)
        if not gothex:
            if error_callback:
                error_callback(_('Invalid token_id hash'))
            return False

        #token name validation
        # if len(token_name) < 1 or len(token_name) > 20:
        #     if error_callback:
        #         error_callback(_('Token name should be 1-20 characters'))
        #     return False


        new_entry = {
            'class'    : token_class,
            'name'     : token_name,
            'decimals' : decimals_divisibility,
        }

        if token_class == "SLP65":
            if group_id is None:
                new_entry['group_id'] = "?"
            else:
                new_entry['group_id'] = group_id

        self.add_token_type(token_id, new_entry)
        self.save_transactions(bool(write_storage))
        return True

    def add_token_from_genesis_tx(self, tx_or_raw, *, error_callback=None, allow_overwrite=True) -> SlpMessage:
        ''' Returns None on failure, optionally calling error_callback
        with a translated UI-suitable error message. Returns a valid
        SlpMessage object on success. In exceptional circumstances (garbage
        inputs), may raise.

        Note that unlike the other add_token_* functions, this version defaults
        to allow_overwrite = True.'''
        tx = tx_or_raw
        if not isinstance(tx, Transaction):
            tx = Transaction(tx)

        def fail(msg):
            if error_callback:
                error_callback(msg)
            return None

        token_id = tx.txid()

        try:
            slpMsg = SlpMessage.parseSlpOutputScript(tx.outputs()[0][1])
        except SlpUnsupportedSlpTokenType as e:
            return fail(_("Unsupported SLP token version/type - %r.")%(e.args[0],))
        except SlpInvalidOutputMessage as e:
            return fail(_("This transaction does not contain a valid SLP message.\nReason: %r.")%(e.args,))
        if slpMsg.transaction_type != 'GENESIS':
            return fail(_("This is an SLP transaction, however it is not a genesis transaction."))

        token_name = slpMsg.op_return_fields['ticker'].decode('utf-8') or slpMsg.op_return_fields['token_name'].decode('utf-8')
        decimals = slpMsg.op_return_fields['decimals']
        token_class = 'SLP%d' % (slpMsg.token_type,)

        if self.add_token_safe(token_class, token_id, token_name, decimals, error_callback=fail, allow_overwrite=allow_overwrite):
            return slpMsg
        else:
            return None

    def save_verified_tx(self, write=False):
        with self.lock:
            self.storage.put('verified_tx3', self.verified_tx)
            if write:
                self.storage.write()

    def clear_history(self):
        with self.lock:
            self.txi = {}
            self.txo = {}
            self.tx_fees = {}
            self.pruned_txo = {}
            self.pruned_txo_values = set()
            self.save_transactions()
            self._addr_bal_cache = {}
            self._history = {}
            self.tx_addr_hist = defaultdict(set)

    @profiler
    def build_reverse_history(self):
        self.tx_addr_hist = defaultdict(set)
        for addr, hist in self._history.items():
            for tx_hash, h in hist:
                self.tx_addr_hist[tx_hash].add(addr)

    @profiler
    def check_history(self):
        save = False
        my_addrs = [addr for addr in self._history if self.is_mine(addr)]

        for addr in set(self._history) - set(my_addrs):
            self._history.pop(addr)
            save = True

        for addr in my_addrs:
            hist = self._history[addr]

            for tx_hash, tx_height in hist:
                if tx_hash in self.pruned_txo_values or self.txi.get(tx_hash) or self.txo.get(tx_hash):
                    continue
                tx = self.transactions.get(tx_hash)
                if tx is not None:
                    self.add_transaction(tx_hash, tx)
                    save = True
        if save:
            self.save_transactions()

    def basename(self):
        return os.path.basename(self.storage.path)

    def save_addresses(self):
        addr_dict = {
            'receiving': [addr.to_storage_string()
                          for addr in self.receiving_addresses],
            'change': [addr.to_storage_string()
                       for addr in self.change_addresses],
        }
        self.storage.put('addresses', addr_dict)

    def load_addresses(self):
        d = self.storage.get('addresses', {})
        if not isinstance(d, dict):
            d = {}
        self.receiving_addresses = Address.from_strings(d.get('receiving', []))
        self.change_addresses = Address.from_strings(d.get('change', []))

    def synchronize(self):
        pass

    def is_deterministic(self):
        return self.keystore.is_deterministic()

    def set_up_to_date(self, up_to_date):
        with self.lock:
            self.up_to_date = up_to_date
        if up_to_date:
            self.save_transactions(write=True)
            # if the verifier is also up to date, persist that too;
            # otherwise it will persist its results when it finishes
            if self.verifier and self.verifier.is_up_to_date():
                self.save_verified_tx(write=True)

    def is_up_to_date(self):
        with self.lock: return self.up_to_date

    def is_fully_settled_down(self):
        ''' Returns True iff the wallet is up to date and its synchronizer
        and verifier aren't busy doing work, and its pruned_txo_values list
        is currently empty.  This is used as a final check by the Qt GUI
        to decide if it should do a final refresh of all tabs in some cases.'''
        with self.lock:
            ret = self.up_to_date
            if ret and self.verifier:
                ret = self.verifier.is_up_to_date()
            if ret and self.synchronizer:
                ret = self.synchronizer.is_up_to_date()
            ret = ret and not self.pruned_txo_values
            return bool(ret)

    def set_label(self, name, text = None):
        with self.lock:
            if isinstance(name, Address):
                name = name.to_storage_string()
            changed = False
            old_text = self.labels.get(name)
            if text:
                text = text.replace("\n", " ")
                if old_text != text:
                    self.labels[name] = text
                    changed = True
            else:
                if old_text:
                    self.labels.pop(name)
                    changed = True

            if changed:
                run_hook('set_label', self, name, text)
                self.storage.put('labels', self.labels)

            return changed

    def invalidate_address_set_cache(self):
        ''' This should be called from functions that add/remove addresses
        from the wallet to ensure the address set caches are empty, in
        particular from ImportedWallets which may add/delete addresses
        thus the length check in is_mine() may not be accurate.
        Deterministic wallets can neglect to call this function since their
        address sets only grow and never shrink and thus the length check
        of is_mine below is sufficient.'''
        self._recv_address_set_cached, self._change_address_set_cached = frozenset(), frozenset()

    def is_mine(self, address):
        ''' Note this method assumes that the entire address set is
        composed of self.get_change_addresses() + self.get_receiving_addresses().
        In subclasses, if that is not the case -- REIMPLEMENT this method! '''
        assert not isinstance(address, str)
        # assumption here is get_receiving_addresses and get_change_addresses
        # are cheap constant-time operations returning a list reference.
        # If that is not the case -- reimplement this function.
        ra, ca = self.get_receiving_addresses(), self.get_change_addresses()
        # Detect if sets changed (addresses added/removed).
        # Note the functions that add/remove addresses should invalidate this
        # cache using invalidate_address_set_cache() above.
        if len(ra) != len(self._recv_address_set_cached):
            # re-create cache if lengths don't match
            self._recv_address_set_cached = frozenset(ra)
        if len(ca) != len(self._change_address_set_cached):
            # re-create cache if lengths don't match
            self._change_address_set_cached = frozenset(ca)
        # Do a 2 x O(logN) lookup using sets rather than 2 x O(N) lookups
        # if we were to use the address lists (this was the previous way).
        # For small wallets it doesn't matter -- but for wallets with 5k or 10k
        # addresses, it starts to add up siince is_mine() is called frequently
        # especially while downloading address history.
        return (address in self._recv_address_set_cached
                    or address in self._change_address_set_cached)

    def is_change(self, address):
        assert not isinstance(address, str)
        ca = self.get_change_addresses()
        if len(ca) != len(self._change_address_set_cached):
            # re-create cache if lengths don't match
            self._change_address_set_cached = frozenset(ca)
        return address in self._change_address_set_cached

    def get_address_index(self, address):
        try:
            return False, self.receiving_addresses.index(address)
        except ValueError:
            pass
        try:
            return True, self.change_addresses.index(address)
        except ValueError:
            pass
        assert not isinstance(address, str)
        raise Exception("Address {} not found".format(address))

    def export_private_key(self, address, password):
        """ extended WIF format """
        if self.is_watching_only():
            return []
        index = self.get_address_index(address)
        pk, compressed = self.keystore.get_private_key(index, password)
        return bitcoin.serialize_privkey(pk, compressed, self.txin_type)

    def get_public_keys(self, address):
        sequence = self.get_address_index(address)
        return self.get_pubkeys(*sequence)

    def add_unverified_tx(self, tx_hash, tx_height):
        with self.lock:
            if tx_height == 0 and tx_hash in self.verified_tx:
                self.verified_tx.pop(tx_hash)
                if self.verifier:
                    self.verifier.merkle_roots.pop(tx_hash, None)

            # tx will be verified only if height > 0
            if tx_hash not in self.verified_tx:
                self.unverified_tx[tx_hash] = tx_height

    def add_verified_tx(self, tx_hash, info):
        # Remove from the unverified map and add to the verified map and
        with self.lock:
            self.unverified_tx.pop(tx_hash, None)
            self.verified_tx[tx_hash] = info  # (tx_height, timestamp, pos)
            height, conf, timestamp = self.get_tx_height(tx_hash)
        self.network.trigger_callback('verified2', self, tx_hash, height, conf, timestamp)

    def get_unverified_txs(self):
        '''Returns a map from tx hash to transaction height'''
        with self.lock:
            return self.unverified_tx.copy()

    def get_unverified_tx_pending_count(self):
        ''' Returns the number of unverified tx's that are confirmed and are
        still in process and should be verified soon.'''
        with self.lock:
            return len([1 for height in self.unverified_tx.values() if height > 0])

    def undo_verifications(self, blockchain, height):
        '''Used by the verifier when a reorg has happened'''
        txs = set()
        with self.lock:
            for tx_hash, item in list(self.verified_tx.items()):
                tx_height, timestamp, pos = item
                if tx_height >= height:
                    header = blockchain.read_header(tx_height)
                    # fixme: use block hash, not timestamp
                    if not header or header.get('timestamp') != timestamp:
                        self.verified_tx.pop(tx_hash, None)
                        txs.add(tx_hash)
        if txs:
            self._addr_bal_cache = {}  # this is probably not necessary -- as the receive_history_callback will invalidate bad cache items -- but just to be paranoid we clear the whole balance cache on reorg anyway as a safety measure
        return txs

    def get_local_height(self):
        """ return last known height if we are offline """
        return self.network.get_local_height() if self.network else self.storage.get('stored_height', 0)

    def get_tx_height(self, tx_hash):
        """ return the height and timestamp of a verified transaction. """
        with self.lock:
            if tx_hash in self.verified_tx:
                height, timestamp, pos = self.verified_tx[tx_hash]
                conf = max(self.get_local_height() - height + 1, 0)
                return height, conf, timestamp
            elif tx_hash in self.unverified_tx:
                height = self.unverified_tx[tx_hash]
                return height, 0, 0
            else:
                return 0, 0, 0

    def get_txpos(self, tx_hash):
        "return position, even if the tx is unverified"
        with self.lock:
            if tx_hash in self.verified_tx:
                height, timestamp, pos = self.verified_tx[tx_hash]
                return height, pos
            elif tx_hash in self.unverified_tx:
                height = self.unverified_tx[tx_hash]
                return (height, 0) if height > 0 else ((1e9 - height), 0)
            else:
                return (1e9+1, 0)

    def is_found(self):
        return any(value for value in self._history.values())

    def get_num_tx(self, address):
        """ return number of transactions where address is involved """
        return len(self.get_address_history(address))

    def get_tx_delta(self, tx_hash, address):
        assert isinstance(address, Address)
        "effect of tx on address"
        # pruned
        if tx_hash in self.pruned_txo_values:
            return None
        delta = 0
        # substract the value of coins sent from address
        d = self.txi.get(tx_hash, {}).get(address, [])
        for n, v in d:
            delta -= v
        # add the value of the coins received at address
        d = self.txo.get(tx_hash, {}).get(address, [])
        for n, v, cb in d:
            delta += v
        return delta

    def get_wallet_delta(self, tx):
        """ effect of tx on wallet """
        is_relevant = False
        is_mine = False
        is_pruned = False
        is_partial = False
        v_in = v_out = v_out_mine = 0
        for item in tx.inputs():
            addr = item['address']
            if self.is_mine(addr):
                is_mine = True
                is_relevant = True
                d = self.txo.get(item['prevout_hash'], {}).get(addr, [])
                for n, v, cb in d:
                    if n == item['prevout_n']:
                        value = v
                        break
                else:
                    value = None
                if value is None:
                    is_pruned = True
                else:
                    v_in += value
            else:
                is_partial = True
        if not is_mine:
            is_partial = False
        for _type, addr, value in tx.outputs():
            v_out += value
            if self.is_mine(addr):
                v_out_mine += value
                is_relevant = True
        if is_pruned:
            # some inputs are mine:
            fee = None
            if is_mine:
                v = v_out_mine - v_out
            else:
                # no input is mine
                v = v_out_mine
        else:
            v = v_out_mine - v_in
            if is_partial:
                # some inputs are mine, but not all
                fee = None
            else:
                # all inputs are mine
                fee = v_in - v_out
        if not is_mine:
            fee = None
        return is_relevant, is_mine, v, fee

    def get_tx_info(self, tx):
        is_relevant, is_mine, v, fee = self.get_wallet_delta(tx)
        exp_n = None
        can_broadcast = False
        label = ''
        height = conf = timestamp = None
        tx_hash = tx.txid()
        if tx.is_complete():
            if tx_hash in self.transactions:
                label = self.get_label(tx_hash)
                height, conf, timestamp = self.get_tx_height(tx_hash)
                if height > 0:
                    if conf:
                        status = ngettext("{conf} confirmation", "{conf} confirmations", conf).format(conf=conf)
                    else:
                        status = _('Not verified')
                else:
                    status = _('Unconfirmed')
                    if fee is None:
                        fee = self.tx_fees.get(tx_hash)
                    if fee and self.network and self.network.config.has_fee_estimates():
                        # NB: this branch will not be taken as has_fee_estimates()
                        # will always return false since we disabled querying
                        # the fee histogram as it's useless for BCH anyway.
                        size = tx.estimated_size()
                        fee_per_kb = fee * 1000 / size
                        exp_n = self.network.config.reverse_dynfee(fee_per_kb)
            else:
                status = _("Signed")
                can_broadcast = self.network is not None
        else:
            s, r = tx.signature_count()
            status = _("Unsigned") if s == 0 else _('Partially signed') + ' (%d/%d)'%(s,r)

        if is_relevant:
            if is_mine:
                if fee is not None:
                    amount = v + fee
                else:
                    amount = v
            else:
                amount = v
        else:
            amount = None

        return tx_hash, status, label, can_broadcast, amount, fee, height, conf, timestamp, exp_n

    def get_addr_io(self, address):
        h = self.get_address_history(address)
        received = {}
        sent = {}
        for tx_hash, height in h:
            l = self.txo.get(tx_hash, {}).get(address, [])
            for n, v, is_cb in l:
                received[tx_hash + ':%d'%n] = (height, v, is_cb)
        for tx_hash, height in h:
            l = self.txi.get(tx_hash, {}).get(address, [])
            for txi, v in l:
                sent[txi] = height
        return received, sent

    def get_slp_token_info(self, tokenid):
        with self.lock:
            return self.tx_tokinfo[tokenid]

    def get_slp_token_baton(self, slpTokenId, cache=True):
        with self.lock:
            slp_txos = copy.deepcopy(self._slp_txo)

        # look for a minting baton
        for addr, addrdict in slp_txos.items():
            for txid, txdict in addrdict.items():
                for idx, txo in txdict.items():
                    if txo['qty'] == 'MINT_BATON' and txo['token_id'] == slpTokenId:
                        try:
                            coins = self.get_slp_utxos(slpTokenId, domain = [addr], exclude_frozen = False, confirmed_only = False, slp_include_baton=True)
                            with self.lock:
                                val = self.tx_tokinfo[txid]['validity']
                                baton_utxo = [ utxo for utxo in coins if utxo['prevout_hash'] == txid and utxo['prevout_n'] == idx and val == 1][0]
                        except IndexError:
                            continue
                        return baton_utxo
        raise SlpNoMintingBatonFound()

    # This method is updated for SLP to prevent tokens from being spent
    # in normal txn or txns with token_id other than the one specified
    def get_addr_utxo(self, address, *, exclude_slp = True):
        coins, spent = self.get_addr_io(address)
        # removes spent coins
        for txi in spent:
            coins.pop(txi)
            # cleanup/detect if the 'frozen coin' was spent and remove it from the frozen coin set
            self.frozen_coins.discard(txi)

        """
        SLP -- removes ALL SLP UTXOs that are either unrelated, or unvalidated
        """
        if exclude_slp:
            with self.lock:
                addrdict = self._slp_txo.get(address,{})
                for txid, txdict in addrdict.items():
                    for idx, txo in txdict.items():
                        coins.pop(txid + ":" + str(idx), None)

        out = {}
        for txo, v in coins.items():
            tx_height, value, is_cb = v
            prevout_hash, prevout_n = txo.split(':')
            x = {
                'address':address,
                'value':value,
                'prevout_n':int(prevout_n),
                'prevout_hash':prevout_hash,
                'height':tx_height,
                'coinbase':is_cb,
                'is_frozen_coin':txo in self.frozen_coins
            }
            out[txo] = x
        return out

    """ SLP -- keeps ONLY SLP UTXOs that are either unrelated, or unvalidated """
    def get_slp_addr_utxo(self, address, slpTokenId, slp_include_invalid=False, slp_include_baton=False, ):
        with self.lock:
            coins, spent = self.get_addr_io(address)
            addrdict = copy.deepcopy(self._slp_txo.get(address,{}))

        # removes spent coins
        for txi in spent:
            coins.pop(txi)
            # cleanup/detect if the 'frozen coin' was spent and remove it from the frozen coin set
            self.frozen_coins.discard(txi)

        coins_to_pop = []
        for coin in coins.items():
            if coin != None:
                txid = coin[0].split(":")[0]
                idx = coin[0].split(":")[1]
                try:
                    slp_txo = addrdict[txid][int(idx)]
                    with self.lock:
                        slp_tx_info = self.tx_tokinfo[txid]
                    # handle special burning modes
                    if slp_txo['token_id'] == slpTokenId:
                        # allow inclusion and possible burning of a valid minting baton
                        if slp_include_baton and slp_txo['qty'] == "MINT_BATON" and slp_tx_info['validity'] == 1:
                            continue
                        # allow inclusion and possible burning of invalid SLP txos
                        if slp_include_invalid and slp_tx_info['validity'] != 0:
                            continue
                    # normal remove any txos that are not valid for this token ID
                    if slp_txo['token_id'] != slpTokenId or slp_tx_info['validity'] != 1 or slp_txo['qty'] == "MINT_BATON":
                        coins_to_pop.append(coin[0])
                except KeyError:
                    coins_to_pop.append(coin[0])

        for c in coins_to_pop:
            coins.pop(c, None)

        out = {}
        for txo, v in coins.items():
            tx_height, value, is_cb = v
            prevout_hash, prevout_n = txo.split(':')
            with self.lock:
                val = self.tx_tokinfo[prevout_hash]['validity']
            x = {
                'address': address,
                'value': value,
                'prevout_n': int(prevout_n),
                'prevout_hash': prevout_hash,
                'height': tx_height,
                'coinbase': is_cb,
                'is_frozen_coin': txo in self.frozen_coins,
                'token_value': addrdict[prevout_hash][int(prevout_n)]['qty'],
                'token_validation_state': val
            }
            out[txo] = x
        return out

    # return the total amount ever received by an address
    def get_addr_received(self, address):
        received, sent = self.get_addr_io(address)
        return sum([v for height, v, is_cb in received.values()])

    # return the balance of a bitcoin address: confirmed and matured, unconfirmed, unmatured
    # Note that 'exclude_frozen_coins = True' only checks for coin-level freezing, not address-level.
    def get_addr_balance(self, address, exclude_frozen_coins=False):
        assert isinstance(address, Address)
        if not exclude_frozen_coins:  # we do not use the cache when excluding frozen coins as frozen status is a dynamic quantity that can change at any time in the UI
            cached = self._addr_bal_cache.get(address)
            if cached is not None:
                return cached
        received, sent = self.get_addr_io(address)
        c = u = x = 0
        had_cb = False
        for txo, (tx_height, v, is_cb) in received.items():
            if exclude_frozen_coins and txo in self.frozen_coins:
                continue
            had_cb = had_cb or is_cb  # remember if this address has ever seen a coinbase txo
            if is_cb and tx_height + COINBASE_MATURITY > self.get_local_height():
                x += v
            elif tx_height > 0:
                c += v
            else:
                u += v
            if txo in sent:
                if sent[txo] > 0:
                    c -= v
                else:
                    u -= v
        result = c, u, x
        if not exclude_frozen_coins and not had_cb:
            # Cache the results.
            # Cache needs to be invalidated if a transaction is added to/
            # removed from addr history.  (See self._addr_bal_cache calls
            # related to this littered throughout this file).
            #
            # Note that as a performance tweak we don't ever cache balances for
            # addresses involving coinbase coins. The rationale being as
            # follows: Caching of balances of the coinbase addresses involves
            # a dynamic quantity: maturity of the coin (which considers the
            # ever-changing block height).
            #
            # There wasn't a good place in this codebase to signal the maturity
            # happening (and thus invalidate the cache entry for the exact
            # address that holds the coinbase coin in question when a new
            # block is found that matures a coinbase coin).
            #
            # In light of that fact, a possible approach would be to invalidate
            # this entire cache when a new block arrives (this is what Electrum
            # does). However, for Electron Cash with its focus on many addresses
            # for future privacy features such as integrated CashShuffle --
            # being notified in the wallet and invalidating the *entire* cache
            # whenever a new block arrives (which is the exact time you do
            # the most GUI refreshing and calling of this function) seems a bit
            # heavy-handed, just for sake of the (relatively rare, for the
            # average user) coinbase-carrying addresses.
            #
            # It's not a huge performance hit for the coinbase addresses to
            # simply not cache their results, and have this function recompute
            # their balance on each call, when you consider that as a
            # consequence of this policy, all the other addresses that are
            # non-coinbase can benefit from a cache that stays valid for longer
            # than 1 block (so long as their balances haven't changed).
            self._addr_bal_cache[address] = result
        return result

    def get_spendable_coins(self, domain, config, isInvoice = False):
        confirmed_only = config.get('confirmed_only', DEFAULT_CONFIRMED_ONLY)
        # if (isInvoice):
        #     confirmed_only = True
        return self.get_utxos(domain=domain, exclude_frozen=True, mature=True, confirmed_only=confirmed_only)

    def get_slp_spendable_coins(self, slpTokenId, domain, config, isInvoice = False):
        confirmed_only = config.get('confirmed_only', False)
        # if (isInvoice):
        #     confirmed_only = True
        return self.get_slp_utxos(slpTokenId, domain=domain, exclude_frozen=True, confirmed_only=confirmed_only)

    def get_slp_coins(self, slpTokenId, domain, config, isInvoice = False):
        confirmed_only = config.get('confirmed_only', False)
        # if (isInvoice):
        #     confirmed_only = True
        return self.get_slp_utxos(slpTokenId, domain=domain, exclude_frozen=False, confirmed_only=confirmed_only)

    def get_slp_token_balance(self, slpTokenId, config):
        valid_token_bal = 0
        unvalidated_token_bal = 0
        invalid_token_bal = 0
        unfrozen_valid_token_bal = 0
        slp_coins = self.get_slp_coins(slpTokenId, None, config)
        for coin in slp_coins:
            txid = coin['prevout_hash']
            validity = self.tx_tokinfo[txid]['validity']
            if validity == 1: # Valid DAG
                valid_token_bal += coin['token_value']
                if not coin['is_frozen_coin'] and coin['address'] not in self.frozen_addresses:
                    unfrozen_valid_token_bal += coin['token_value']
            elif validity > 1: # Invalid DAG (2=bad slpmessage, 3=inputs lack enough tokens / missing mint baton, 4=change token_type or bad NFT parent)
                invalid_token_bal += coin['token_value']
            elif validity == 0: # Unknown DAG status (should be in processing queue)
                unvalidated_token_bal += coin['token_value']
        return (valid_token_bal, unvalidated_token_bal, invalid_token_bal, unfrozen_valid_token_bal, valid_token_bal - unfrozen_valid_token_bal)

    def get_utxos(self, *, domain = None, exclude_frozen = False, mature = False, confirmed_only = False, exclude_slp = True):
        ''' Note that exclude_frozen = True checks for BOTH address-level and coin-level frozen status. '''
        coins = []
        if domain is None:
            domain = self.get_addresses()
        if exclude_frozen:
            domain = set(domain) - self.frozen_addresses
        for addr in domain:
            utxos = self.get_addr_utxo(addr, exclude_slp=exclude_slp)
            for x in utxos.values():
                if exclude_frozen and x['is_frozen_coin']:
                    continue
                if confirmed_only and x['height'] <= 0:
                    continue
                if mature and x['coinbase'] and x['height'] + COINBASE_MATURITY > self.get_local_height():
                    continue
                coins.append(x)
                continue
        return coins

    def get_slp_utxos(self, slpTokenId, *, domain = None, exclude_frozen = False, confirmed_only = False, slp_include_invalid=False, slp_include_baton=False):
        ''' Note that exclude_frozen = True checks for BOTH address-level and coin-level frozen status. '''
        coins = []
        if domain is None:
            domain = self.get_addresses()
        if exclude_frozen:
            domain = set(domain) - self.frozen_addresses
        for addr in domain:
            utxos = self.get_slp_addr_utxo(addr, slpTokenId, slp_include_invalid=slp_include_invalid, slp_include_baton=slp_include_baton)
            for x in utxos.values():
                if exclude_frozen and x['is_frozen_coin']:
                    continue
                if confirmed_only and x['height'] <= 0:
                    continue
                coins.append(x)
                continue
        return coins

    def dummy_address(self):
        return self.get_receiving_addresses()[0]

    def get_addresses(self):
        return self.get_receiving_addresses() + self.get_change_addresses()

    def get_frozen_balance(self):
        if not self.frozen_coins:
            # performance short-cut -- get the balance of the frozen address set only IFF we don't have any frozen coins
            return self.get_balance(self.frozen_addresses)
        # otherwise, do this more costly calculation...
        cc_no_f, uu_no_f, xx_no_f = self.get_balance(None, exclude_frozen_coins = True, exclude_frozen_addresses = True)
        cc_all, uu_all, xx_all = self.get_balance(None, exclude_frozen_coins = False, exclude_frozen_addresses = False)
        return (cc_all-cc_no_f), (uu_all-uu_no_f), (xx_all-xx_no_f)

    def get_slp_locked_balance(self):
        bch = 0
        with self.lock:
            for addr, addrdict in self._slp_txo.items():
                _, spent = self.get_addr_io(addr)
                for txid, txdict in addrdict.items():
                    for idx, txo in txdict.items():
                        if (txid + ":" + str(idx)) in spent:
                            continue
                        try:
                            for i, a, _ in self.txo[txid][addr]:
                                if i == idx:
                                    bch+=a
                        except KeyError:
                            pass
        return bch

    def get_balance(self, domain=None, exclude_frozen_coins=False, exclude_frozen_addresses=False):
        if domain is None:
            domain = self.get_addresses()
        if exclude_frozen_addresses:
            domain = set(domain) - self.frozen_addresses
        cc = uu = xx = 0
        for addr in domain:
            c, u, x = self.get_addr_balance(addr, exclude_frozen_coins)
            cc += c
            uu += u
            xx += x
        return cc, uu, xx

    def get_address_history(self, address):
        assert isinstance(address, Address)
        return self._history.get(address, [])

    def _clean_pruned_txo_thread(self):
        ''' Runs in the thread self.pruned_txo_cleaner_thread which is only
        active if self.network. Cleans the self.pruned_txo dict and the
        self.pruned_txo_values set of spends that are not relevant to the
        wallet. The processing below is needed because as of 9/16/2019, Electron
        Cash temporarily puts all spends that pass through add_transaction and
        have an unparseable address (txi['address'] is None) into the dict
        self.pruned_txo. This is necessary for handling tx's with esoteric p2sh
        scriptSigs and detecting balance changes properly for txins
        containing such scriptSigs. See #895. '''
        def deser(ser):
            prevout_hash, prevout_n = ser.split(':')
            prevout_n = int(prevout_n)
            return prevout_hash, prevout_n
        def mkser(prevout_hash, prevout_n):
            return f'{prevout_hash}:{prevout_n}'
        def rm(ser, pruned_too=True, *, tup = None):
            h, n = tup or deser(ser)  # tup arg is for performance when caller already knows the info (avoid a redundant .split on ':')
            s = txid_n[h]
            s.discard(n)
            if not s:
                txid_n.pop(h, None)
            if pruned_too:
                with self.lock:
                    tx_hash = self.pruned_txo.pop(ser, None)
                    self.pruned_txo_values.discard(tx_hash)
        def add(ser):
            prevout_hash, prevout_n = deser(ser)
            txid_n[prevout_hash].add(prevout_n)
        def keep_running():
            return bool(self.network and self.pruned_txo_cleaner_thread is me)
        def can_do_work():
            return bool(txid_n and self.is_up_to_date())
        debug = False  # set this to true here to get more verbose output
        me = threading.current_thread()
        q = me.q
        me.txid_n = txid_n = defaultdict(set)  # dict of prevout_hash -> set of prevout_n (int)
        last = time.time()
        try:
            self.print_error(f"{me.name}: thread started")
            with self.lock:
                # Setup -- grab whatever was already in pruned_txo at thread
                # start
                for ser in self.pruned_txo:
                    h, n = deser(ser)
                    txid_n[h].add(n)
            while keep_running():
                try:
                    ser = q.get(timeout=5.0 if can_do_work() else 20.0)
                    if ser is None:
                        # quit thread
                        return
                    if ser.startswith('r_'):
                        # remove requested
                        rm(ser[2:], False)
                    else:
                        # ser was added
                        add(ser)
                    del ser
                except queue.Empty:
                    pass
                if not can_do_work():
                    continue
                t0 = time.time()
                if t0 - last < 1.0:  # run no more often than once per second
                    continue
                last = t0
                defunct_ct = 0
                for prevout_hash, s in txid_n.copy().items():
                    for prevout_n in s.copy():
                        ser = mkser(prevout_hash, prevout_n)
                        with self.lock:
                            defunct = ser not in self.pruned_txo
                        if defunct:
                            #self.print_error(f"{me.name}: skipping already-cleaned", ser)
                            rm(ser, False, tup=(prevout_hash, prevout_n))
                            defunct_ct += 1
                            continue
                if defunct_ct and debug:
                    self.print_error(f"{me.name}: DEBUG", defunct_ct, "defunct txos removed in", time.time()-t0, "secs")
                ct = 0
                for prevout_hash, s in txid_n.copy().items():
                    try:
                        with self.lock:
                            tx = self.transactions.get(prevout_hash)
                        if tx is None:
                            tx = Transaction.tx_cache_get(prevout_hash)
                        if isinstance(tx, Transaction):
                            tx = Transaction(tx.raw)  # take a copy
                        else:
                            if debug: self.print_error(f"{me.name}: DEBUG retrieving txid", prevout_hash, "...")
                            t1 = time.time()
                            tx = Transaction(self.network.synchronous_get(('blockchain.transaction.get', [prevout_hash])))
                            if debug: self.print_error(f"{me.name}: DEBUG network retrieve took", time.time()-t1, "secs")
                            # Paranoia; intended side effect of the below assert
                            # is to also deserialize the tx (by calling the slow
                            # .txid()) which ensures the tx from the server
                            # is not junk.
                            assert prevout_hash == tx.txid(), "txid mismatch"
                            Transaction.tx_cache_put(tx, prevout_hash)  # will cache a copy
                    except Exception as e:
                        self.print_error(f"{me.name}: Error retrieving txid", prevout_hash, ":", repr(e))
                        if not keep_running():  # in case we got a network timeout *and* the wallet was closed
                            return
                        continue
                    if not keep_running():
                        return
                    for prevout_n in s.copy():
                        ser = mkser(prevout_hash, prevout_n)
                        try:
                            txo = tx.outputs()[prevout_n]
                        except IndexError:
                            self.print_error(f"{me.name}: ERROR -- could not find output", ser)
                            rm(ser, True, tup=(prevout_hash, prevout_n))
                            continue
                        _typ, addr, v = txo
                        rm_pruned_too = False
                        with self.lock:
                            mine = self.is_mine(addr)
                            if not mine and ser in self.pruned_txo:
                                ct += 1
                                rm_pruned_too = True
                        rm(ser, rm_pruned_too, tup=(prevout_hash, prevout_n))
                        if rm_pruned_too and debug:
                            self.print_error(f"{me.name}: DEBUG removed", ser)
                if ct:
                    with self.lock:
                        # Save changes to storage -- this is cheap and doesn't
                        # actually write to file yet, just flags storage as
                        # 'dirty' for when wallet.storage.write() is called
                        # later.
                        self.storage.put('pruned_txo', self.pruned_txo)
                    self.print_error(f"{me.name}: removed", ct,
                                     "(non-relevant) pruned_txo's in",
                                     f'{time.time()-t0:3.2f}', "seconds")
        except:
            import traceback
            self.print_error(f"{me.name}:", traceback.format_exc())
            raise
        finally:
            self.print_error(f"{me.name}: thread exiting")

    def add_transaction(self, tx_hash, tx):
        if not tx.inputs():
            # bad tx came in off the wire -- all 0's or something, see #987
            self.print_error("add_transaction: WARNING a tx came in from the network with 0 inputs! Bad server? Ignoring tx:", tx_hash)
            return
        is_coinbase = tx.inputs()[0]['type'] == 'coinbase'
        with self.lock:
            # HELPER FUNCTIONS
            def add_to_self_txi(tx_hash, addr, ser, v):
                ''' addr must be 'is_mine' '''
                d = self.txi.get(tx_hash)
                if d is None:
                    self.txi[tx_hash] = d = {}
                l = d.get(addr)
                if l is None:
                    d[addr] = l = []
                l.append((ser, v))
            def find_in_self_txo(prevout_hash: str, prevout_n: int) -> tuple:
                ''' Returns a tuple of the (Address,value) for a given
                prevout_hash:prevout_n, or (None, None) if not found. If valid
                return, the Address object is found by scanning self.txo. The
                lookup below is relatively fast in practice even on pathological
                wallets. '''
                dd = self.txo.get(prevout_hash, {})
                for addr2, item in dd.items():
                    for n, v, is_cb in item:
                        if n == prevout_n:
                            return addr2, v
                return (None, None)
            def txin_get_info(txin):
                prevout_hash = txi['prevout_hash']
                prevout_n = txi['prevout_n']
                ser = prevout_hash + ':%d'%prevout_n
                return prevout_hash, prevout_n, ser
            def put_pruned_txo(ser, tx_hash):
                self.pruned_txo[ser] = tx_hash
                self.pruned_txo_values.add(tx_hash)
                t = self.pruned_txo_cleaner_thread
                if t and t.q: t.q.put(ser)
            def pop_pruned_txo(ser):
                next_tx = self.pruned_txo.pop(ser, None)
                if next_tx:
                    self.pruned_txo_values.discard(next_tx)
                    t = self.pruned_txo_cleaner_thread
                    if t and t.q: t.q.put('r_' + ser)  # notify of removal
                return next_tx
            # /HELPER FUNCTIONS

            # add inputs
            self.txi[tx_hash] = d = {}
            for txi in tx.inputs():
                if txi['type'] == 'coinbase':
                    continue
                addr = txi.get('address')
                # find value from prev output
                if self.is_mine(addr):
                    prevout_hash, prevout_n, ser = txin_get_info(txi)
                    dd = self.txo.get(prevout_hash, {})
                    for n, v, is_cb in dd.get(addr, []):
                        if n == prevout_n:
                            add_to_self_txi(tx_hash, addr, ser, v)
                            break
                    else:
                        # Coin's spend tx came in before its receive tx: flag
                        # the spend for when the receive tx will arrive into
                        # this function later.
                        put_pruned_txo(ser, tx_hash)
                    self._addr_bal_cache.pop(addr, None)  # invalidate cache entry
                    del dd, prevout_hash, prevout_n, ser
                elif addr is None:
                    # Unknown/unparsed address.. may be a strange p2sh scriptSig
                    # Try and find it in txout's if it's one of ours.
                    # See issue #895.
                    prevout_hash, prevout_n, ser = txin_get_info(txi)
                    # Find address in self.txo for this prevout_hash:prevout_n
                    addr2, v = find_in_self_txo(prevout_hash, prevout_n)
                    if addr2 is not None and self.is_mine(addr2):
                        add_to_self_txi(tx_hash, addr2, ser, v)
                        self._addr_bal_cache.pop(addr2, None)  # invalidate cache entry
                    else:
                        # Not found in self.txo. It may still be one of ours
                        # however since tx's can come in out of order due to
                        # CTOR, etc, and self.txo may not have it yet. So we
                        # flag the spend now, and when the out-of-order prevout
                        # tx comes in later for this input (if it's indeed one
                        # of ours), the real address for this input will get
                        # picked up then in the "add outputs" section below in
                        # this function. At that point, self.txi will be
                        # properly updated to indicate the coin in question was
                        # spent via an add_to_self_txi call.
                        #
                        # If it's *not* one of ours, however, the below will
                        # grow pruned_txo with an irrelevant entry. However, the
                        # irrelevant entry will eventually be reaped and removed
                        # by the self.pruned_txo_cleaner_thread which runs
                        # periodically in the background.
                        put_pruned_txo(ser, tx_hash)
                    del addr2, v, prevout_hash, prevout_n, ser
            # don't keep empty entries in self.txi
            if not d:
                self.txi.pop(tx_hash, None)

            # add outputs
            self.txo[tx_hash] = d = {}
            for n, txo in enumerate(tx.outputs()):
                ser = tx_hash + ':%d'%n
                _type, addr, v = txo
                mine = False
                if self.is_mine(addr):
                    # add coin to self.txo since it's mine.
                    mine = True
                    l = d.get(addr)
                    if l is None:
                        d[addr] = l = []
                    l.append((n, v, is_coinbase))
                    del l
                    self._addr_bal_cache.pop(addr, None)  # invalidate cache entry
                # give v to txi that spends me
                next_tx = pop_pruned_txo(ser)
                if next_tx is not None and mine:
                    add_to_self_txi(next_tx, addr, ser, v)
            # don't keep empty entries in self.txo
            if not d:
                self.txo.pop(tx_hash, None)


            # save
            self.transactions[tx_hash] = tx

            ### SLP: Handle incoming SLP transaction outputs here
            self.handleSlpTransaction(tx_hash, tx)

    """
    Callers are expected to take lock(s). We take no locks
    """
    def handleSlpTransaction(self, tx_hash, tx):
        txouts = tx.outputs()

        try:
            slpMsg = SlpMessage.parseSlpOutputScript(txouts[0][1])
        except SlpUnsupportedSlpTokenType as e:
            token_type = 'SLP%d'%(e.args[0],)
            for i, (_type, addr, _) in enumerate(txouts):
                if _type == TYPE_ADDRESS and self.is_mine(addr):
                    self._slp_txo[addr][tx_hash][i] = {
                            'type': token_type,
                            'qty': None,
                            'token_id': None,
                            }
            return
        except (SlpParsingError, IndexError, OpreturnError):
            return

        if slpMsg.transaction_type == 'SEND':
            token_id_hex = slpMsg.op_return_fields['token_id_hex']
            # truncate outputs list
            amounts = slpMsg.op_return_fields['token_output'][:len(txouts)]
            for i, qty in enumerate(amounts):
                _type, addr, _ = txouts[i]
                if _type == TYPE_ADDRESS and qty > 0 and self.is_mine(addr):
                    self._slp_txo[addr][tx_hash][i] = {
                            'type': 'SLP%d'%(slpMsg.token_type,),
                            'token_id': token_id_hex,
                            'qty': qty,
                            }
        elif slpMsg.transaction_type == 'GENESIS':
            token_id_hex = tx_hash
            try:
                _type, addr, _ = txouts[1]
                if _type == TYPE_ADDRESS:
                    if slpMsg.op_return_fields['initial_token_mint_quantity'] > 0 and self.is_mine(addr):
                        self._slp_txo[addr][tx_hash][1] = {
                                'type': 'SLP%d'%(slpMsg.token_type,),
                                'token_id': token_id_hex,
                                'qty': slpMsg.op_return_fields['initial_token_mint_quantity'],
                            }
                    if slpMsg.op_return_fields['mint_baton_vout'] is not None:
                        i = slpMsg.op_return_fields['mint_baton_vout']
                        _type, addr, _ = txouts[i]
                        if _type == TYPE_ADDRESS:
                            self._slp_txo[addr][tx_hash][i] = {
                                    'type': 'SLP%d'%(slpMsg.token_type,),
                                    'token_id': token_id_hex,
                                    'qty': 'MINT_BATON',
                                }
            except IndexError: # if too few outputs (compared to mint_baton_vout)
                pass
        elif slpMsg.transaction_type == "MINT":
            token_id_hex = slpMsg.op_return_fields['token_id_hex']
            try:
                _type, addr, _ = txouts[1]
                if _type == TYPE_ADDRESS:
                    if slpMsg.op_return_fields['additional_token_quantity'] > 0 and self.is_mine(addr):
                        self._slp_txo[addr][tx_hash][1] = {
                                'type': 'SLP%d'%(slpMsg.token_type,),
                                'token_id': token_id_hex,
                                'qty': slpMsg.op_return_fields['additional_token_quantity'],
                            }
                    if slpMsg.op_return_fields['mint_baton_vout'] is not None:
                        i = slpMsg.op_return_fields['mint_baton_vout']
                        _type, addr, _ = txouts[i]
                        if _type == TYPE_ADDRESS:
                            self._slp_txo[addr][tx_hash][i] = {
                                    'type': 'SLP%d'%(slpMsg.token_type,),
                                    'token_id': token_id_hex,
                                    'qty': 'MINT_BATON',
                                }
            except IndexError: # if too few outputs (compared to mint_baton_vout)
                pass
        elif slpMsg.transaction_type == 'COMMIT':
            # ignore COMMs, they aren't producing any tokens.
            return
        else:
            raise RuntimeError(slpMsg.transaction_type)

        # On receiving a new SEND, MINT, or GENESIS always add entry to token_types if wallet hasn't seen tokenId yet
        if slpMsg.transaction_type in [ 'SEND', 'MINT', 'GENESIS' ]:
            if slpMsg.transaction_type == 'GENESIS':
                tokenid = tx_hash
            else:
                tokenid = slpMsg.op_return_fields['token_id_hex']
            new_token = True
            for k, v in self.tx_tokinfo.items():
                try:
                    if v['token_id'] == tokenid:
                        new_token = False
                except KeyError:
                    pass
            if new_token and tokenid not in self.token_types:
                tty = { 'class': 'SLP%d'%(slpMsg.token_type,),
                        'decimals': "?",
                        'name': 'unknown-' + tokenid[:6]
                        }
                if slpMsg.token_type == 65:
                    tty['group_id'] = "?"
                self.token_types[tokenid] = tty

        # Always add entry to tx_tokinfo
        tti = { 'type':'SLP%d'%(slpMsg.token_type,),
                'transaction_type':slpMsg.transaction_type,
                'token_id': token_id_hex,
                'validity': 0,
                }
        self.tx_tokinfo[tx_hash] = tti

        if self.is_slp: # Only start up validation if SLP enabled
            self.slp_check_validation(tx_hash, tx)

    def slp_check_validation(self, tx_hash, tx):
        """ Callers are expected to take lock(s). We take no locks """
        tti = self.tx_tokinfo[tx_hash]
        try:
            is_new = self.token_types[tti['token_id']]['decimals'] == '?'
        except:
            is_new = False
        if tti['validity'] == 0 and tti['token_id'] in self.token_types and not is_new and tti['type'] in ['SLP1','SLP65','SLP129']:
            def callback(job):
                (txid,node), = job.nodes.items()
                val = node.validity
                tti['validity'] = val
                slp_gs_mgr.slp_validity_signal.emit(txid, val)

            if tti['type'] in ['SLP1']:
                job = self.slp_graph_0x01.make_job(tx, self, self.network,
                                                        debug=2 if is_verbose else 1,  # set debug=2 here to see the verbose dag when running with -v
                                                        reset=False)
            elif tti['type'] in ['SLP65', 'SLP129']:
                job = self.slp_graph_0x01_nft.make_job(tx, self, self.network, nft_type=tti['type'],
                                                        debug=2 if is_verbose else 1,  # set debug=2 here to see the verbose dag when running with -v
                                                        reset=False)

            if job is not None:
                job.add_callback(callback)
                # This was commented out because it spammed the log so badly
                # it impacted performance. SLP validation can create a *lot* of jobs!
                #finalization_print_error(job, f"[{self.basename()}] Job for {tx_hash} type {tti['type']} finalized")

    def rebuild_slp(self,):
        """Wipe away old SLP transaction data and rerun on the entire tx set.

        """
        with self.lock:
            self._slp_txo = defaultdict(lambda: defaultdict(dict))
            self.tx_tokinfo = {}
            for txid, tx in self.transactions.items():
                self.handleSlpTransaction(txid, tx)

    def remove_transaction(self, tx_hash):
        with self.lock:
            self.print_error("removing tx from history", tx_hash)
            # Note that we don't actually remove the tx_hash from
            # self.transactions, but instead rely on the unreferenced tx being
            # removed the next time the wallet is loaded in self.load_transactions()

            for ser, hh in list(self.pruned_txo.items()):
                if hh == tx_hash:
                    self.pruned_txo.pop(ser)
                    self.pruned_txo_values.discard(hh)
            # add tx to pruned_txo, and undo the txi addition
            for next_tx, dd in self.txi.items():
                for addr, l in list(dd.items()):
                    ll = l[:]
                    for item in ll:
                        ser, v = item
                        prev_hash, prev_n = ser.split(':')
                        if prev_hash == tx_hash:
                            self._addr_bal_cache.pop(addr, None)  # invalidate cache entry
                            l.remove(item)
                            self.pruned_txo[ser] = next_tx
                            self.pruned_txo_values.add(next_tx)
                    if l == []:
                        dd.pop(addr)
                    else:
                        dd[addr] = l

            # invalidate addr_bal_cache for outputs involving this tx
            d = self.txo.get(tx_hash, {})
            for addr in d:
                self._addr_bal_cache.pop(addr, None)  # invalidate cache entry

            self.txi.pop(tx_hash, None)
            self.txo.pop(tx_hash, None)
            self.tx_fees.pop(tx_hash, None)
            self.tx_tokinfo[tx_hash] = {}

            for addr, addrdict in self._slp_txo.items():
                if tx_hash in addrdict: addrdict[tx_hash] = {}

    def receive_tx_callback(self, tx_hash, tx, tx_height):
        self.add_transaction(tx_hash, tx)
        self.add_unverified_tx(tx_hash, tx_height)

    def receive_history_callback(self, addr, hist, tx_fees):
        with self.lock:
            old_hist = self.get_address_history(addr)
            for tx_hash, height in old_hist:
                if (tx_hash, height) not in hist:
                    s = self.tx_addr_hist.get(tx_hash)
                    if s:
                        s.discard(addr)
                    if not s:
                        # if no address references this tx anymore, kill it
                        # from txi/txo dicts.
                        if s is not None:
                            # We won't keep empty sets around.
                            self.tx_addr_hist.pop(tx_hash)
                        # note this call doesn't actually remove the tx from
                        # storage, it merely removes it from the self.txi
                        # and self.txo dicts
                        self.remove_transaction(tx_hash)
            self._addr_bal_cache.pop(addr, None)  # unconditionally invalidate cache entry
            self._history[addr] = hist

            for tx_hash, tx_height in hist:
                # add it in case it was previously unconfirmed
                self.add_unverified_tx(tx_hash, tx_height)
                # add reference in tx_addr_hist
                self.tx_addr_hist[tx_hash].add(addr)
                # if addr is new, we have to recompute txi and txo
                tx = self.transactions.get(tx_hash)
                if tx is not None and self.txi.get(tx_hash, {}).get(addr) is None and self.txo.get(tx_hash, {}).get(addr) is None:
                    self.add_transaction(tx_hash, tx)

            # Store fees
            self.tx_fees.update(tx_fees)

        if self.network:
            self.network.trigger_callback('on_history', self)

    def get_slp_history(self, domain=None, validities_considered=(None,0,1)):
        history = []
        histories = self.get_slp_histories(domain=domain, validities_considered=validities_considered)
        # Take separate token histories and flatten them, then sort them.
        for token_id,t_history in histories.items():
            for tx_hash, height, conf, timestamp, delta in t_history:
                history.append((tx_hash, height, conf, timestamp, delta, token_id))
        history.sort(key = lambda x: self.get_txpos(x[0]), reverse=True)

        return history

    def get_slp_histories(self, domain=None, validities_considered=(0,1)):
        # Based on get_history.
        # We return a dict of histories, one history per token_id.

        # get domain
        if domain is None:
            domain = self.get_addresses()

        #1. Big iteration to find all deltas and put them in the right place.
        token_tx_deltas = defaultdict(lambda: defaultdict(int)) # defaultdict of defaultdicts of ints :)
        for addr in domain:
            h = self.get_address_history(addr)
            with self.lock:
                addrslptxo = self._slp_txo[addr]

                for tx_hash, height in h:
                    if tx_hash in self.pruned_txo.values():
                        continue
                    tti = self.tx_tokinfo.get(tx_hash)
                    if tti and tti['validity'] in validities_considered:
                        txdict = addrslptxo.get(tx_hash,{})

                        for idx,d in txdict.items():
                            if isinstance(d['qty'],int):
                                token_tx_deltas[d['token_id']][tx_hash] += d['qty']  # received!

                    # scan over all txi's, trying to find if they were tokens, which tokens, and how much
                    # (note that non-SLP txes can spend (burn) SLP --- and SLP of tokenA can burn tokenB)
                    for n, _ in self.txi.get(tx_hash, {}).get(addr, ()):
                        prevtxid, prevout_str = n.rsplit(':',1)
                        tti = self.tx_tokinfo.get(prevtxid)
                        if not (tti and tti['validity'] in validities_considered):
                            continue
                        prevout = int(prevout_str)

                        d = addrslptxo.get(prevtxid,{}).get(prevout,{})
                        if isinstance(d.get('qty',None),int):
                            token_tx_deltas[d['token_id']][tx_hash] -= d['qty']  # received!

        # 2. create history (no sorting needed since balances won't be computed)
        histories = {}
        for token_id, tx_deltas in token_tx_deltas.items():
            history = histories[token_id] = []
            for tx_hash in tx_deltas:
                delta = tx_deltas[tx_hash]
                height, conf, timestamp = self.get_tx_height(tx_hash)
                history.append((tx_hash, height, conf, timestamp, delta))

        # 3. At this point we could compute running balances, but let's not.

        return histories

    def get_history(self, domain=None, *, reverse=False):
        # get domain
        if domain is None:
            domain = self.get_addresses()
        # 1. Get the history of each address in the domain, maintain the
        #    delta of a tx as the sum of its deltas on domain addresses
        tx_deltas = defaultdict(int)
        for addr in domain:
            h = self.get_address_history(addr)
            for tx_hash, height in h:
                delta = self.get_tx_delta(tx_hash, addr)
                if delta is None or tx_deltas[tx_hash] is None:
                    tx_deltas[tx_hash] = None
                else:
                    tx_deltas[tx_hash] += delta

        # 2. create sorted history
        history = []
        for tx_hash in tx_deltas:
            delta = tx_deltas[tx_hash]
            height, conf, timestamp = self.get_tx_height(tx_hash)
            history.append((tx_hash, height, conf, timestamp, delta))
        history.sort(key = lambda x: self.get_txpos(x[0]), reverse=True)

        # 3. add balance
        c, u, x = self.get_balance(domain)
        balance = c + u + x
        h2 = []
        for tx_hash, height, conf, timestamp, delta in history:
            h2.append((tx_hash, height, conf, timestamp, delta, balance))
            if balance is None or delta is None:
                balance = None
            else:
                balance -= delta
        if not reverse:
            h2.reverse()

        return h2

    def export_history(self, domain=None, from_timestamp=None, to_timestamp=None, fx=None,
                       show_addresses=False, decimal_point=8,
                       *, fee_calc_timeout=10.0, download_inputs=False,
                       progress_callback=None):
        ''' Export history. Used by RPC & GUI.

        Arg notes:
        - `fee_calc_timeout` is used when computing the fee (which is done
          asynchronously in another thread) to limit the total amount of time in
          seconds spent waiting for fee calculation. The timeout is a total time
          allotment for this function call. (The reason the fee calc can take a
          long time is for some pathological tx's, it is very slow to calculate
          fee as it involves deserializing prevout_tx from the wallet, for each
          input).
        - `download_inputs`, if True, will allow for more accurate fee data to
          be exported with the history by using the Transaction class input
          fetcher to download *all* prevout_hash tx's for inputs (even for
          inputs not in wallet). This feature requires self.network (ie, we need
          to be online) otherwise it will behave as if download_inputs=False.
        - `progress_callback`, if specified, is a callback which receives a
          single float argument in the range [0.0,1.0] indicating how far along
          the history export is going. This is intended for interop with GUI
          code. Node the progress callback is not guaranteed to be called in the
          context of the main thread, therefore GUI code should use appropriate
          signals/slots to update the GUI with progress info.

        Note on side effects: This function may update self.tx_fees. Rationale:
        it will spend some time trying very hard to calculate accurate fees by
        examining prevout_tx's (leveraging the fetch_input_data code in the
        Transaction class). As such, it is worthwhile to cache the results in
        self.tx_fees, which gets saved to wallet storage. This is not very
        demanding on storage as even for very large wallets with huge histories,
        tx_fees does not use more than a few hundred kb of space. '''
        from .util import timestamp_to_datetime
        # we save copies of tx's we deserialize to this temp dict because we do
        # *not* want to deserialize tx's in wallet.transactoins since that
        # wastes memory
        local_tx_cache = {}
        # some helpers for this function
        t0 = time.time()
        def time_remaining(): return max(fee_calc_timeout - (time.time()-t0), 0)
        class MissingTx(RuntimeError):
            ''' Can happen in rare circumstances if wallet history is being
            radically reorged by network thread while we are in this code. '''
        def get_tx(tx_hash):
            ''' Try to get a tx from wallet, then from the Transaction class
            cache if that fails. In either case it deserializes the copy and
            puts the deserialized tx in local stack dict local_tx_cache. The
            reason we don't deserialize the tx's from self.transactions is that
            we do not want to keep deserialized tx's in memory. The
            self.transactions dict should contain just raw tx's (not
            deserialized). Deserialized tx's eat on the order of 10x the memory
            because because of the Python lists, dict, etc they contain, per
            instance. '''
            tx = local_tx_cache.get(tx_hash)
            if tx:
                return tx
            tx = Transaction.tx_cache_get(tx_hash)
            if not tx:
                tx = copy.deepcopy(self.transactions.get(tx_hash))
            if tx:
                tx.deserialize()
                local_tx_cache[tx_hash] = tx
            else:
                raise MissingTx(f'txid {tx_hash} dropped out of wallet history while exporting')
            return tx
        def try_calc_fee(tx_hash):
            ''' Try to calc fee from cheapest to most expensive calculation.
            Ultimately asks the transaction class to look at prevouts in wallet and uses
            that scheme as a last (more CPU intensive) resort. '''
            fee = self.tx_fees.get(tx_hash)
            if fee is not None:
                return fee
            def do_get_fee(tx_hash):
                tx = get_tx(tx_hash)
                def try_get_fee(tx):
                    try: return tx.get_fee()
                    except InputValueMissing: pass
                fee = try_get_fee(tx)
                t_remain = time_remaining()
                if fee is None and t_remain:
                    q = queue.Queue()
                    def done():
                        q.put(1)
                    tx.fetch_input_data(self, use_network=bool(download_inputs), done_callback=done)
                    try: q.get(timeout=t_remain)
                    except queue.Empty: pass
                    fee = try_get_fee(tx)
                return fee
            fee = do_get_fee(tx_hash)
            if fee is not None:
                self.tx_fees[tx_hash] = fee  # save fee to wallet if we bothered to dl/calculate it.
            return fee
        def fmt_amt(v, is_diff):
            if v is None:
                return '--'
            return format_satoshis(v, decimal_point=decimal_point,
                                   is_diff=is_diff)

        # grab history
        h = self.get_history(domain, reverse=True)
        out = []

        n, l = 0, max(1, float(len(h)))
        for tx_hash, height, conf, timestamp, value, balance in h:
            if progress_callback:
                progress_callback(n/l)
            n += 1
            timestamp_safe = timestamp
            if timestamp is None:
                timestamp_safe = time.time()  # set it to "now" so below code doesn't explode.
            if from_timestamp and timestamp_safe < from_timestamp:
                continue
            if to_timestamp and timestamp_safe >= to_timestamp:
                continue
            try:
                fee = try_calc_fee(tx_hash)
            except MissingTx as e:
                self.print_error(str(e))
                continue
            item = {
                'txid'          : tx_hash,
                'height'        : height,
                'confirmations' : conf,
                'timestamp'     : timestamp_safe,
                'value'         : fmt_amt(value, is_diff=True),
                'fee'           : fmt_amt(fee, is_diff=False),
                'balance'       : fmt_amt(balance, is_diff=False),
            }
            if item['height'] > 0:
                date_str = format_time(timestamp) if timestamp is not None else _("unverified")
            else:
                date_str = _("unconfirmed")
            item['date'] = date_str
            try:
                # Defensive programming.. sanitize label.
                # The below ensures strings are utf8-encodable. We do this
                # as a paranoia measure.
                item['label'] = self.get_label(tx_hash).encode(encoding='utf-8', errors='replace').decode(encoding='utf-8', errors='replace')
            except UnicodeError:
                self.print_error(f"Warning: could not export label for {tx_hash}, defaulting to ???")
                item['label'] = "???"
            if show_addresses:
                tx = get_tx(tx_hash)
                input_addresses = []
                output_addresses = []
                for x in tx.inputs():
                    if x['type'] == 'coinbase': continue
                    addr = x.get('address')
                    if addr == None: continue
                    input_addresses.append(addr.to_ui_string())
                for _type, addr, v in tx.outputs():
                    output_addresses.append(addr.to_ui_string())
                item['input_addresses'] = input_addresses
                item['output_addresses'] = output_addresses
            if fx is not None:
                date = timestamp_to_datetime(timestamp_safe)
                item['fiat_value'] = fx.historical_value_str(value, date)
                item['fiat_balance'] = fx.historical_value_str(balance, date)
                item['fiat_fee'] = fx.historical_value_str(fee, date)
            out.append(item)
        if progress_callback:
            progress_callback(1.0)  # indicate done, just in case client code expects a 1.0 in order to detect completion
        return out

    def get_label(self, tx_hash):
        label = self.labels.get(tx_hash, '')
        if label is '':
            label = self.get_default_label(tx_hash)
        return label

    def get_default_label(self, tx_hash):
        if not self.txi.get(tx_hash):
            d = self.txo.get(tx_hash, {})
            labels = []
            for addr in list(d.keys()):  # use a copy to avoid possibility of dict changing during iteration, see #1328
                label = self.labels.get(addr.to_storage_string())
                if label:
                    labels.append(label)
            return ', '.join(labels)
        return ''

    def get_tx_status(self, tx_hash, height, conf, timestamp):
        if conf == 0:
            tx = self.transactions.get(tx_hash)
            if not tx:
                return 3, 'unknown'
            fee = self.tx_fees.get(tx_hash)
            # we disable fee estimates in BCH for now.
            #if fee and self.network and self.network.config.has_fee_estimates():
            #    size = len(tx.raw)/2
            #    low_fee = int(self.network.config.dynfee(0)*size/1000)
            #    is_lowfee = fee < low_fee * 0.5
            #else:
            #    is_lowfee = False
            # and instead if it's less than 1.0 sats/B we flag it as low_fee
            try:
                # NB len(tx.raw) is 2x the byte size as it's hex encoded.
                is_lowfee = int(fee) / (int(len(tx.raw)) / 2.0) < 1.0  # if less than 1.0 sats/B, complain. otherwise don't.
            except (TypeError, ValueError):  # If for some reason fee was None or invalid, just pass on through.
                is_lowfee = False
            # /
            if height < 0:
                status = 0
            elif height == 0 and is_lowfee:
                status = 1
            elif height == 0:
                status = 2
            else:
                status = 3
        else:
            status = 3 + min(conf, 6)
        time_str = format_time(timestamp) if timestamp else _("unknown")
        status_str = _(TX_STATUS[status]) if status < 4 else time_str
        return status, status_str

    def relayfee(self):
        return relayfee(self.network)

    def dust_threshold(self):
        return dust_threshold(self.network)

    def check_sufficient_slp_balance(self, slpMessage, config):
        if self.is_slp:
            if slpMessage.transaction_type == 'SEND':
                total_token_out = sum(slpMessage.op_return_fields['token_output'])
                valid_token_balance, _, _, valid_unfrozen_token_balance, _ = self.get_slp_token_balance(slpMessage.op_return_fields['token_id_hex'], config)
                if total_token_out > valid_token_balance:
                    raise NotEnoughFundsSlp()
                elif total_token_out > valid_unfrozen_token_balance:
                    raise NotEnoughUnfrozenFundsSlp()

    def make_unsigned_transaction(self, inputs, outputs, config, fixed_fee=None, change_addr=None, sign_schnorr=None, *, mandatory_coins=[]):
        ''' sign_schnorr flag controls whether to mark the tx as signing with
        schnorr or not. Specify either a bool, or set the flag to 'None' to use
        whatever the wallet is configured to use from the GUI '''
        sign_schnorr = self.is_schnorr_enabled() if sign_schnorr is None else bool(sign_schnorr)
        # check outputs
        i_max = None
        for i, o in enumerate(outputs):
            _type, data, value = o
            if value == '!':
                if i_max is not None:
                    raise BaseException("More than one output set to spend max")
                i_max = i

        # Avoid index-out-of-range with inputs[0] below
        if not inputs:
            raise NotEnoughFunds()

        if fixed_fee is None and config.fee_per_kb() is None:
            raise BaseException('Dynamic fee estimates not available')

        for item in inputs:
            self.add_input_info(item)

        for item in mandatory_coins:
            self.add_input_info(item)

        # change address
        if change_addr:
            change_addrs = [change_addr]
        else:
            addrs = self.get_change_addresses()[-self.gap_limit_for_change:]
            if self.use_change and addrs:
                # New change addresses are created only after a few
                # confirmations.  Select the unused addresses within the
                # gap limit; if none take one at random
                change_addrs = [addr for addr in addrs if
                                self.get_num_tx(addr) == 0]
                if not change_addrs:
                    change_addrs = [random.choice(addrs)]
            else:
                change_addrs = [self.get_addresses()[0]]

        assert all(isinstance(addr, Address) for addr in change_addrs)

        # Fee estimator
        if fixed_fee is None:
            fee_estimator = config.estimate_fee
        else:
            fee_estimator = lambda size: fixed_fee

        if i_max is None:
            # Let the coin chooser select the coins to spend
            max_change = self.max_change_outputs if self.multiple_change else 1
            coin_chooser = coinchooser.CoinChooserPrivacy()
            tx = coin_chooser.make_tx(inputs, outputs, change_addrs[:max_change],
                                        fee_estimator, self.dust_threshold(), sign_schnorr=sign_schnorr,
                                        mandatory_coins=mandatory_coins)
        else:
            inputs = mandatory_coins + inputs
            sendable = sum(map(lambda x:x['value'], inputs))
            _type, data, value = outputs[i_max]
            outputs[i_max] = (_type, data, 0)
            tx = Transaction.from_io(inputs, outputs, sign_schnorr=sign_schnorr)
            fee = fee_estimator(tx.estimated_size())
            amount = max(0, sendable - tx.output_value() - fee)
            outputs[i_max] = (_type, data, amount)
            tx = Transaction.from_io(inputs, outputs, sign_schnorr=sign_schnorr)

        # If user tries to send too big of a fee (more than 50 sat/byte), stop them from shooting themselves in the foot
        tx_in_bytes=tx.estimated_size()
        fee_in_satoshis=tx.get_fee()
        sats_per_byte=fee_in_satoshis/tx_in_bytes
        if (sats_per_byte > 50):
            raise ExcessiveFee()
            return

        # Sort the inputs and outputs deterministically
        if not mandatory_coins:
            tx.BIP_LI01_sort()

        # Timelock tx to current height.
        locktime = self.get_local_height()
        if locktime == -1: # We have no local height data (no headers synced).
            locktime = 0
        tx.locktime = locktime
        run_hook('make_unsigned_transaction', self, tx)
        return tx

    def make_unsigned_transaction_for_bitcoinfiles(self, inputs, outputs, config, fixed_fee=None, change_addr=None, sign_schnorr=None):
        sign_schnorr = self.is_schnorr_enabled() if sign_schnorr is None else bool(sign_schnorr)
        # check outputs
        i_max = None
        for i, o in enumerate(outputs):
            _type, data, value = o
            if value == '!':
                if i_max is not None:
                    raise BaseException("More than one output set to spend max")
                i_max = i

        # Avoid index-out-of-range with inputs[0] below
        if not inputs:
            raise NotEnoughFunds()


        if fixed_fee is None and config.fee_per_kb() is None:
            raise BaseException('Dynamic fee estimates not available')

        for item in inputs:
            self.add_input_info_for_bitcoinfiles(item)

        # change address
        if change_addr:
            change_addrs = [change_addr]
        else:
            addrs = self.get_change_addresses()[-self.gap_limit_for_change:]
            if self.use_change and addrs:
                # New change addresses are created only after a few
                # confirmations.  Select the unused addresses within the
                # gap limit; if none take one at random
                change_addrs = [addr for addr in addrs if
                                self.get_num_tx(addr) == 0]
                if not change_addrs:
                    change_addrs = [random.choice(addrs)]
            else:
                change_addrs = [inputs[0]['address']]

        assert all(isinstance(addr, Address) for addr in change_addrs)

        # Fee estimator
        if fixed_fee is None:
            fee_estimator = config.estimate_fee
        else:
            fee_estimator = lambda size: fixed_fee

        if i_max is None:
            # Let the coin chooser select the coins to spend
            max_change = self.max_change_outputs if self.multiple_change else 1
            coin_chooser = coinchooser.CoinChooserPrivacy()
            # determine if this transaction should utilize all available inputs
            tx = coin_chooser.make_tx(inputs, outputs, change_addrs[:max_change],
                                      fee_estimator, self.dust_threshold(), sign_schnorr=sign_schnorr)
        else:
            sendable = sum(map(lambda x:x['value'], inputs))
            _type, data, value = outputs[i_max]
            outputs[i_max] = (_type, data, 0)
            tx = Transaction.from_io(inputs, outputs, sign_schnorr=sign_schnorr)
            fee = fee_estimator(tx.estimated_size())
            amount = max(0, sendable - tx.output_value() - fee)
            outputs[i_max] = (_type, data, amount)
            tx = Transaction.from_io(inputs, outputs, sign_schnorr=sign_schnorr)

        # If user tries to send too big of a fee (more than 50 sat/byte), stop them from shooting themselves in the foot
        tx_in_bytes=tx.estimated_size()
        fee_in_satoshis=tx.get_fee()
        sats_per_byte=fee_in_satoshis/tx_in_bytes
        if (sats_per_byte > 50):
            raise ExcessiveFee()
            return

        # Timelock tx to current height.
        locktime = self.get_local_height()
        if locktime == -1: # We have no local height data (no headers synced).
            locktime = 0
        tx.locktime = locktime
        run_hook('make_unsigned_transaction', self, tx)
        return tx

    def mktx(self, outputs, password, config, fee=None, change_addr=None, domain=None, sign_schnorr=None):
        coins = self.get_spendable_coins(domain, config)
        tx = self.make_unsigned_transaction(coins, outputs, config, fee, change_addr, sign_schnorr=sign_schnorr)
        self.sign_transaction(tx, password)
        return tx

    def is_frozen(self, addr):
        ''' Address-level frozen query. Note: this is set/unset independent of 'coin' level freezing. '''
        assert isinstance(addr, Address)
        return addr in self.frozen_addresses

    def is_frozen_coin(self, utxo):
        ''' 'coin' level frozen query. `utxo' is a prevout:n string, or a dict as returned from get_utxos().
            Note: this is set/unset independent of 'address' level freezing. '''
        assert isinstance(utxo, (str, dict))
        if isinstance(utxo, dict):
            ret = ("{}:{}".format(utxo['prevout_hash'], utxo['prevout_n'])) in self.frozen_coins
            if ret != utxo['is_frozen_coin']:
                self.print_error("*** WARNING: utxo has stale is_frozen_coin flag")
                utxo['is_frozen_coin'] = ret # update stale flag
            return ret
        return utxo in self.frozen_coins

    def set_frozen_state(self, addrs, freeze):
        ''' Set frozen state of the addresses to FREEZE, True or False
            Note that address-level freezing is set/unset independent of coin-level freezing, however both must
            be satisfied for a coin to be defined as spendable.. '''
        if all(self.is_mine(addr) for addr in addrs):
            if freeze:
                self.frozen_addresses |= set(addrs)
            else:
                self.frozen_addresses -= set(addrs)
            frozen_addresses = [addr.to_storage_string()
                                for addr in self.frozen_addresses]
            self.storage.put('frozen_addresses', frozen_addresses)
            return True
        return False

    def set_frozen_coin_state(self, utxos, freeze):
        ''' Set frozen state of the COINS to FREEZE, True or False.
            utxos is a (possibly mixed) list of either "prevout:n" strings and/or coin-dicts as returned from get_utxos().
            Note that if passing prevout:n strings as input, 'is_mine()' status is not checked for the specified coin.
            Also note that coin-level freezing is set/unset independent of address-level freezing, however both must
            be satisfied for a coin to be defined as spendable. '''
        ok = 0
        for utxo in utxos:
            if isinstance(utxo, str):
                if freeze:
                    self.frozen_coins |= { utxo }
                else:
                    self.frozen_coins -= { utxo }
                ok += 1
            elif isinstance(utxo, dict) and self.is_mine(utxo['address']):
                txo = "{}:{}".format(utxo['prevout_hash'], utxo['prevout_n'])
                if freeze:
                    self.frozen_coins |= { txo }
                else:
                    self.frozen_coins -= { txo }
                utxo['is_frozen_coin'] = bool(freeze)
                ok += 1
        if ok:
            self.storage.put('frozen_coins', list(self.frozen_coins))
        return ok

    def prepare_for_verifier(self):
        # review transactions that are in the history
        for addr, hist in self._history.items():
            for tx_hash, tx_height in hist:
                # add it in case it was previously unconfirmed
                self.add_unverified_tx(tx_hash, tx_height)

        # if we are on a pruning server, remove unverified transactions
        with self.lock:
            vr = list(self.verified_tx.keys()) + list(self.unverified_tx.keys())
        for tx_hash in list(self.transactions):
            if tx_hash not in vr:
                self.print_error("removing transaction", tx_hash)
                self.transactions.pop(tx_hash)

    def _slp_callback_on_status(self, event, *args):
        if self.is_slp and args[0] == 'connected':
            self.activate_slp()

    def start_threads(self, network):
        self.network = network
        if self.network is not None:
            if self.is_slp:
                # Note: it's important that SLP data structures are defined
                # before the network (SPV/Synchronizer) callbacks are installed
                # otherwise we may receive a tx from the network thread
                # before SLP objects are properly constructed.
                self.slp_graph_0x01 = slp_validator_0x01.shared_context
                self.slp_graph_0x01_nft = slp_validator_0x01_nft1.shared_context_nft1
                self.activate_slp()
                self.network.register_callback(self._slp_callback_on_status, ['status'])
            self.start_pruned_txo_cleaner_thread()
            self.prepare_for_verifier()
            self.verifier = SPV(self.network, self)
            self.synchronizer = Synchronizer(self, network)
            finalization_print_error(self.verifier, "[{}.{}] finalized".format(self.diagnostic_name(), self.verifier.diagnostic_name()))
            finalization_print_error(self.synchronizer, "[{}.{}] finalized".format(self.diagnostic_name(), self.synchronizer.diagnostic_name()))
            network.add_jobs([self.verifier, self.synchronizer])
        else:
            self.verifier = None
            self.synchronizer = None

    def stop_threads(self):
        if self.network:
            # Note: syncrhonizer and verifier will remove themselves from the
            # network thread the next time they run, as a result of the below
            # release() calls.
            # It is done this way (as opposed to an immediate clean-up here)
            # because these objects need to do thier clean-up actions in a
            # thread-safe fashion from within the thread where they normally
            # operate on their data structures.
            self.synchronizer.release()
            self.verifier.release()
            self.synchronizer = None
            self.verifier = None
            self.stop_pruned_txo_cleaner_thread()
            # Now no references to the syncronizer or verifier
            # remain so they will be GC-ed
            if self.is_slp:
                # NB: it's important this be done here after network
                # callbacks are torn down in the above lines.
                self.network.unregister_callback(self._slp_callback_on_status)
                jobs_stopped = self.slp_graph_0x01.stop_all_for_wallet(self, timeout=2.0)
                self.print_error("Stopped", len(jobs_stopped), "slp_0x01 jobs")
                #jobs_stopped = self.slp_graph_0x01_nft.stop_all_for_wallet(self)
                #self.print_error("Stopped", len(jobs_stopped), "slp_0x01_nft jobs")
                self.slp_graph_0x01_nft.kill()
                self.slp_graph_0x01, self.slp_graph_0x01_nft = None, None
            self.storage.put('stored_height', self.get_local_height())
        self.save_transactions()
        self.save_verified_tx()
        self.storage.write()

    def start_pruned_txo_cleaner_thread(self):
        self.pruned_txo_cleaner_thread = threading.Thread(target=self._clean_pruned_txo_thread, daemon=True, name='clean_pruned_txo_thread')
        self.pruned_txo_cleaner_thread.q = queue.Queue()
        self.pruned_txo_cleaner_thread.start()

    def stop_pruned_txo_cleaner_thread(self):
        t = self.pruned_txo_cleaner_thread
        self.pruned_txo_cleaner_thread = None  # this also signals a stop
        if t and t.is_alive():
            t.q.put(None)  # signal stop
            # if the join times out, it's ok. it means the thread was stuck in
            # a network call and it will eventually exit.
            t.join(timeout=3.0)

    def wait_until_synchronized(self, callback=None):
        def wait_for_wallet():
            self.set_up_to_date(False)
            while not self.is_up_to_date():
                if callback:
                    msg = "%s\n%s %d"%(
                        _("Please wait..."),
                        _("Addresses generated:"),
                        len(self.addresses(True)))
                    callback(msg)
                time.sleep(0.1)
        def wait_for_network():
            while not self.network.is_connected():
                if callback:
                    msg = "%s \n" % (_("Connecting..."))
                    callback(msg)
                time.sleep(0.1)
        # wait until we are connected, because the user
        # might have selected another server
        if self.network:
            wait_for_network()
            wait_for_wallet()
        else:
            self.synchronize()

    def can_export(self):
        return not self.is_watching_only() and hasattr(self.keystore, 'get_private_key')

    def is_used(self, address):
        return self.get_address_history(address) and self.is_empty(address)

    def is_empty(self, address):
        assert isinstance(address, Address)
        return not any(self.get_addr_balance(address))

    def address_is_old(self, address, age_limit=2):
        age = -1
        local_height = self.get_local_height()
        for tx_hash, tx_height in self.get_address_history(address):
            if tx_height == 0:
                tx_age = 0
            else:
                tx_age = local_height - tx_height + 1
            if tx_age > age:
                age = tx_age
            if age > age_limit:
                break # ok, it's old. not need to keep looping
        return age > age_limit

    def cpfp(self, tx, fee, sign_schnorr=None):
        ''' sign_schnorr is a bool or None for auto '''
        sign_schnorr = self.is_schnorr_enabled() if sign_schnorr is None else bool(sign_schnorr)
        txid = tx.txid()
        for i, o in enumerate(tx.outputs()):
            otype, address, value = o
            if otype == TYPE_ADDRESS and self.is_mine(address):
                break
        else:
            return
        coins = self.get_addr_utxo(address)
        item = coins.get(txid+':%d'%i)
        if not item:
            return
        self.add_input_info(item)
        inputs = [item]
        outputs = [(TYPE_ADDRESS, address, value - fee)]
        locktime = self.get_local_height()
        # note: no need to call tx.BIP_LI01_sort() here - single input/output
        return Transaction.from_io(inputs, outputs, locktime=locktime, sign_schnorr=sign_schnorr)

    def add_input_info(self, txin):
        address = txin['address']
        if self.is_mine(address):
            txin['type'] = self.get_txin_type(address)
            # Bitcoin Cash needs value to sign
            received, spent = self.get_addr_io(address)
            item = received.get(txin['prevout_hash']+':%d'%txin['prevout_n'])
            tx_height, value, is_cb = item
            txin['value'] = value
            self.add_input_sig_info(txin, address)

    def add_input_info_for_bitcoinfiles(self, txin):
        address = txin['address']
        if self.is_mine(address):
            txin['type'] = self.get_txin_type(address)
            self.add_input_sig_info(txin, address)

    def can_sign(self, tx):
        if tx.is_complete():
            return False
        for k in self.get_keystores():
            # setup "wallet advice" so Xpub wallets know how to sign 'fd' type tx inputs
            # by giving them the sequence number ahead of time
            if isinstance(k, BIP32_KeyStore):
                for txin in tx.inputs():
                    for x_pubkey in txin['x_pubkeys']:
                        if not x_pubkey[0:2] in ['02', '03', '04']:
                            _, addr = xpubkey_to_address(x_pubkey)
                            try:
                                c, index = self.get_address_index(addr)
                            except:
                                continue
                        else:
                            c, index = k.scan_for_pubkey_index(x_pubkey)
                            if c == 0:
                                addr = self.receiving_addresses[index]
                            elif c == 1:
                                addr = self.change_addresses[index]
                        if index is not None:
                            k.set_wallet_advice(addr, [c,index])
            if k.can_sign(tx):
                return True
        return False

    def get_input_tx(self, tx_hash):
        # First look up an input transaction in the wallet where it
        # will likely be.  If co-signing a transaction it may not have
        # all the input txs, in which case we ask the network.
        tx = self.transactions.get(tx_hash)
        if not tx and self.network:
            request = ('blockchain.transaction.get', [tx_hash])
            tx = Transaction(self.network.synchronous_get(request))
        return tx

    def add_input_values_to_tx(self, tx):
        """ add input values to the tx, for signing"""
        for txin in tx.inputs():
            if 'value' not in txin:
                inputtx = self.get_input_tx(txin['prevout_hash'])
                if inputtx is not None:
                    out_zero, out_addr, out_val = inputtx.outputs()[txin['prevout_n']]
                    txin['value'] = out_val
                    txin['prev_tx'] = inputtx   # may be needed by hardware wallets

    def add_hw_info(self, tx):
        # add previous tx for hw wallets, if needed and not already there
        if any([(isinstance(k, Hardware_KeyStore) and k.can_sign(tx) and k.needs_prevtx()) for k in self.get_keystores()]):
            for txin in tx.inputs():
                if 'prev_tx' not in txin:
                    txin['prev_tx'] = self.get_input_tx(txin['prevout_hash'])
        # add output info for hw wallets
        info = {}
        xpubs = self.get_master_public_keys()
        for txout in tx.outputs():
            _type, addr, amount = txout
            if self.is_change(addr):
                index = self.get_address_index(addr)
                pubkeys = self.get_public_keys(addr)
                # sort xpubs using the order of pubkeys
                sorted_pubkeys, sorted_xpubs = zip(*sorted(zip(pubkeys, xpubs)))
                info[addr] = index, sorted_xpubs, self.m if isinstance(self, Multisig_Wallet) else None, self.txin_type
        tx.output_info = info

    def sign_transaction(self, tx, password, *, use_cache=False):
        """ Sign a transaction, requires password (may be None for password-less
        wallets). If `use_cache` is enabled then signing will be much faster.

        For transactions with N inputs and M outputs, calculating all sighashes
        takes only O(N + M) with the cache, as opposed to O(N^2 + NM) without
        the cache.

        Warning: If you modify non-signature parts of the transaction
        afterwards, do not use `use_cache`! """

        if self.is_watching_only():
            return
        # add input values for signing
        self.add_input_values_to_tx(tx)
        # hardware wallets require extra info
        if any([(isinstance(k, Hardware_KeyStore) and k.can_sign(tx)) for k in self.get_keystores()]):
            self.add_hw_info(tx)
        # sign
        for k in self.get_keystores():
            try:
                if k.can_sign(tx):
                    k.sign_transaction(tx, password, use_cache=use_cache)
            except UserCancelled:
                continue

    def get_unused_addresses(self, *, for_change=False, frozen_ok=True):
        # fixme: use slots from expired requests
        with self.lock:
            domain = self.get_receiving_addresses() if not for_change else (self.get_change_addresses() or self.get_receiving_addresses())
            return [addr for addr in domain
                    if not self.get_address_history(addr)
                    and addr not in self.receive_requests
                    and (frozen_ok or addr not in self.frozen_addresses)]

    def get_unused_address(self, *, for_change=False, frozen_ok=True):
        addrs = self.get_unused_addresses(for_change=for_change, frozen_ok=frozen_ok)
        if addrs:
            return addrs[0]

    def get_receiving_address(self, *, frozen_ok=True):
        '''Returns a receiving address or None.'''
        domain = self.get_unused_addresses(frozen_ok=frozen_ok)
        if not domain:
                domain = [a for a in self.get_receiving_addresses()
                          if frozen_ok or a not in self.frozen_addresses]
        if domain:
            return domain[0]

    def get_payment_status(self, address, amount):
        local_height = self.get_local_height()
        received, sent = self.get_addr_io(address)
        l = []
        for txo, x in received.items():
            h, v, is_cb = x
            txid, n = txo.split(':')
            info = self.verified_tx.get(txid)
            if info:
                tx_height, timestamp, pos = info
                conf = local_height - tx_height
            else:
                conf = 0
            l.append((conf, v))
        vsum = 0
        for conf, v in reversed(sorted(l)):
            vsum += v
            if vsum >= amount:
                return True, conf
        return False, None

    def has_payment_request(self, addr):
        ''' Returns True iff Address addr has any extant payment requests
        (even if expired), False otherwise. '''
        assert isinstance(addr, Address)
        return bool(self.receive_requests.get(addr))

    def get_payment_request(self, addr, config):
        assert isinstance(addr, Address)
        r = self.receive_requests.get(addr)
        if not r:
            return
        out = copy.copy(r)
        addr_text = addr.to_ui_string()
        if r.get('token_id', None):
            amount_text = str(r['amount'])
        else:
            amount_text = format_satoshis(r['amount'])
        if addr.FMT_UI == addr.FMT_CASHADDR:
            out['URI'] = '{}:{}?amount={}'.format(networks.net.CASHADDR_PREFIX,
                                                  addr_text, amount_text)
        elif addr.FMT_UI == addr.FMT_SLPADDR:
            if r.get('token_id', None):
                token_id = r['token_id']
                out['URI'] = '{}:{}?amount={}-{}'.format(networks.net.SLPADDR_PREFIX,
                                                         addr_text, amount_text, token_id)
            else:
                out['URI'] = '{}:{}?amount={}'.format(networks.net.SLPADDR_PREFIX,
                                                      addr_text, amount_text)
        status, conf = self.get_request_status(addr)
        out['status'] = status
        if conf is not None:
            out['confirmations'] = conf
        # check if bip70 file exists
        rdir = config.get('requests_dir')
        if rdir:
            key = out.get('id', addr.to_storage_string())
            path = os.path.join(rdir, 'req', key[0], key[1], key)
            if os.path.exists(path):
                baseurl = 'file://' + rdir
                rewrite = config.get('url_rewrite')
                if rewrite:
                    baseurl = baseurl.replace(*rewrite)
                out['request_url'] = os.path.join(baseurl, 'req', key[0], key[1], key, key)
                out['URI'] += '&r=' + out['request_url']
                if not 'index_url' in out:
                    out['index_url'] = os.path.join(baseurl, 'index.html') + '?id=' + key
                websocket_server_announce = config.get('websocket_server_announce')
                if websocket_server_announce:
                    out['websocket_server'] = websocket_server_announce
                else:
                    out['websocket_server'] = config.get('websocket_server', 'localhost')
                websocket_port_announce = config.get('websocket_port_announce')
                if websocket_port_announce:
                    out['websocket_port'] = websocket_port_announce
                else:
                    out['websocket_port'] = config.get('websocket_port', 9999)
        return out

    def get_request_status(self, key):
        r = self.receive_requests.get(key)
        if r is None:
            return PR_UNKNOWN
        address = r['address']
        amount = r.get('amount')
        timestamp = r.get('time', 0)
        if timestamp and type(timestamp) != int:
            timestamp = 0
        expiration = r.get('exp')
        if expiration and type(expiration) != int:
            expiration = 0
        conf = None
        if amount:
            if self.up_to_date:
                paid, conf = self.get_payment_status(address, amount)
                status = PR_PAID if paid else PR_UNPAID
                if status == PR_UNPAID and expiration is not None and time.time() > timestamp + expiration:
                    status = PR_EXPIRED
            else:
                status = PR_UNKNOWN
        else:
            status = PR_UNKNOWN
        return status, conf

    def make_payment_request(self, addr, amount, message, expiration=None, *,
                             op_return=None, op_return_raw=None, payment_url=None, token_id=None, index_url=None):
        assert isinstance(addr, Address)
        if op_return and op_return_raw:
            raise ValueError("both op_return and op_return_raw cannot be specified as arguments to make_payment_request")
        timestamp = int(time.time())
        _id = bh2u(Hash(addr.to_storage_string() + "%d" % timestamp))[0:10]
        d = {
            'time': timestamp,
            'amount': amount,
            'exp': expiration,
            'address': addr,
            'memo': message,
            'id': _id
        }
        if token_id:
            d['token_id'] = token_id
        if payment_url:
            d['payment_url'] = payment_url + "/" + _id
        if index_url:
            d['index_url'] = index_url + "/" + _id
        if op_return:
            d['op_return'] = op_return
        if op_return_raw:
            d['op_return_raw'] = op_return_raw
        return d

    def serialize_request(self, r):
        result = r.copy()
        result['address'] = r['address'].to_storage_string()
        return result

    def save_payment_requests(self):
        def delete_address(value):
            del value['address']
            return value

        requests = {addr.to_storage_string() : delete_address(value.copy())
                    for addr, value in self.receive_requests.items()}
        self.storage.put('payment_requests', requests)
        self.storage.write()

    def sign_payment_request(self, key, alias, alias_addr, password):
        req = self.receive_requests.get(key)
        alias_privkey = self.export_private_key(alias_addr, password)
        pr = paymentrequest.make_unsigned_request(req)
        paymentrequest.sign_request_with_alias(pr, alias, alias_privkey)
        req['name'] = pr.pki_data
        req['sig'] = bh2u(pr.signature)
        self.receive_requests[key] = req
        self.save_payment_requests()

    def add_payment_request(self, req, config, set_address_label=True):
        addr = req['address']
        addr_text = addr.to_storage_string()
        amount = req['amount']
        message = req['memo']
        self.receive_requests[addr] = req
        self.save_payment_requests()
        if set_address_label:
            self.set_label(addr_text, message) # should be a default label

        rdir = config.get('requests_dir')
        if rdir and amount is not None:
            key = req.get('id', addr_text)
            pr = paymentrequest.make_request(config, req)
            path = os.path.join(rdir, 'req', key[0], key[1], key)
            if not os.path.exists(path):
                try:
                    os.makedirs(path)
                except OSError as exc:
                    if exc.errno != errno.EEXIST:
                        raise
            with open(os.path.join(path, key), 'wb') as f:
                f.write(pr.SerializeToString())
            # reload
            req = self.get_payment_request(addr, config)
            req['address'] = req['address'].to_ui_string()
            with open(os.path.join(path, key + '.json'), 'w', encoding='utf-8') as f:
                f.write(json.dumps(req))

    def remove_payment_request(self, addr, config, clear_address_label_if_no_tx=True):
        if isinstance(addr, str):
            addr = Address.from_string(addr)
        if addr not in self.receive_requests:
            return False
        r = self.receive_requests.pop(addr)
        if clear_address_label_if_no_tx and not self.get_address_history(addr):
            memo = r.get('memo')
            # clear it only if the user didn't overwrite it with something else
            if memo and memo == self.labels.get(addr.to_storage_string()):
                self.set_label(addr, None)

        rdir = config.get('requests_dir')
        if rdir:
            key = r.get('id', addr.to_storage_string())
            for s in ['.json', '']:
                n = os.path.join(rdir, 'req', key[0], key[1], key, key + s)
                if os.path.exists(n):
                    os.unlink(n)
        self.save_payment_requests()
        return True

    def get_sorted_requests(self, config):
        m = map(lambda x: self.get_payment_request(x, config), self.receive_requests.keys())
        try:
            def f(x):
                try:
                    addr = x['address']
                    return self.get_address_index(addr) or addr
                except:
                    return addr
            return sorted(m, key=f)
        except TypeError:
            # See issue #1231 -- can get inhomogenous results in the above
            # sorting function due to the 'or addr' possible return.
            # This can happen if addresses for some reason drop out of wallet
            # while, say, the history rescan is running and it can't yet find
            # an address index for an address.  In that case we will
            # return an unsorted list to the caller.
            return list(m)

    def get_fingerprint(self):
        raise NotImplementedError()

    def can_import_privkey(self):
        return False

    def can_import_address(self):
        return False

    def can_delete_address(self):
        return False

    def is_multisig(self):
        # Subclass Multisig_Wallet overrides this
        return False

    def is_hardware(self):
        return any([isinstance(k, Hardware_KeyStore) for k in self.get_keystores()])

    def add_address(self, address):
        assert isinstance(address, Address)
        self._addr_bal_cache.pop(address, None)  # paranoia, not really necessary -- just want to maintain the invariant that when we modify address history below we invalidate cache.
        self.invalidate_address_set_cache()
        if address not in self._history:
            self._history[address] = []
        if self.synchronizer:
            self.synchronizer.add(address)

    def has_password(self):
        return self.storage.get('use_encryption', False)

    def check_password(self, password):
        self.keystore.check_password(password)

    def sign_message(self, address, message, password):
        index = self.get_address_index(address)
        return self.keystore.sign_message(index, message, password)

    def decrypt_message(self, pubkey, message, password):
        addr = self.pubkeys_to_address(pubkey)
        index = self.get_address_index(addr)
        return self.keystore.decrypt_message(index, message, password)

    def rebuild_history(self):
        ''' This is an advanced function for use in the GUI when the user
        wants to resynch the whole wallet from scratch, preserving labels
        and contacts.  Right now this implementation is beta-quality but
        appears to work great for the most part. '''
        if not self.network or not self.network.is_connected():
            raise RuntimeError('Refusing to rebuild wallet without a valid server connection!')
        if not self.synchronizer or not self.verifier:
            raise RuntimeError('Refusing to rebuild a stopped wallet!')
        network = self.network
        self.stop_threads()
        do_addr_save = False
        with self.lock:
            self.transactions.clear(); self.unverified_tx.clear(); self.verified_tx.clear()
            self._slp_txo.clear(); self.slpv1_validity.clear(); self.token_types.clear(); self.tx_tokinfo.clear()
            self.clear_history()
            if isinstance(self, Standard_Wallet):
                # reset the address list to default too, just in case. New synchronizer will pick up the addresses again.
                self.receiving_addresses, self.change_addresses = self.receiving_addresses[:self.gap_limit], self.change_addresses[:self.gap_limit_for_change]
                do_addr_save = True
            self.invalidate_address_set_cache()
        if do_addr_save:
            self.save_addresses()
        self.save_transactions()
        self.save_verified_tx()
        self.storage.write()
        self.start_threads(network)
        self.network.trigger_callback('wallet_updated', self)

    def is_schnorr_possible(self, reason: list = None) -> bool:
        ''' Returns True if this wallet type is compatible.
        `reason` is an optional list where you would like a translated string
        of why Schnorr isn't possible placed (on False return). '''
        ok = bool(not self.is_multisig() and not self.is_hardware())
        if not ok and isinstance(reason, list):
            reason.insert(0, _('Schnorr signatures are disabled for this wallet type.'))
        return ok

    def is_schnorr_enabled(self) -> bool:
        ''' Returns whether schnorr is enabled AND possible for this wallet.
        Schnorr is enabled per-wallet. '''
        if not self.is_schnorr_possible():
            # Short-circuit out of here -- it's not even possible with this
            # wallet type.
            return False
        ss_cfg = self.storage.get('sign_schnorr', None)
        if ss_cfg is None:
            # Schnorr was not set in config; figure out intelligent defaults,
            # preferring Schnorr if it's at least as fast as ECDSA (based on
            # which libs user has installed). Note for watching-only we default
            # to off if unspecified regardless, to not break compatibility
            # with air-gapped signing systems that have older EC installed
            # on the signing system. This is to avoid underpaying fees if
            # signing system doesn't use Schnorr.  We can turn on default
            # Schnorr on watching-only sometime in the future after enough
            # time has passed that air-gapped systems are unlikely to not
            # have Schnorr enabled by default.
            # TO DO: Finish refactor of txn serialized format to handle this
            # case better!
            if (not self.is_watching_only()
                    and (schnorr.has_fast_sign()
                         or not ecc_fast.is_using_fast_ecc())):
                # Prefer Schnorr, all things being equal.
                # - If not watching-only & schnorr possible AND
                # - Either Schnorr is fast sign (native, ABC's secp256k1),
                #   so use it by default
                # - Or both ECDSA & Schnorr are slow (non-native);
                #   so use Schnorr in that case as well
                ss_cfg = 2
            else:
                # This branch is reached if Schnorr is slow but ECDSA is fast
                # (core's secp256k1 lib was found which lacks Schnorr) -- so we
                # default it to off. Also if watching only we default off.
                ss_cfg = 0
        return bool(ss_cfg)

    def set_schnorr_enabled(self, b: bool):
        ''' Enable schnorr for this wallet. Note that if Schnorr is not possible,
        (due to missing libs or invalid wallet type) is_schnorr_enabled() will
        still return False after calling this function with a True argument. '''
        # Note: we will have '1' at some point in the future which will mean:
        # 'ask me per tx', so for now True -> 2.
        self.storage.put('sign_schnorr', 2 if b else 0)


class Simple_Wallet(Abstract_Wallet):
    # wallet with a single keystore

    def get_keystore(self):
        return self.keystore

    def get_keystores(self):
        return [self.keystore]

    def is_watching_only(self):
        return self.keystore.is_watching_only()

    def can_change_password(self):
        return self.keystore.can_change_password()

    def update_password(self, old_pw, new_pw, encrypt=False):
        if old_pw is None and self.has_password():
            raise InvalidPassword()
        if self.keystore is not None and self.keystore.can_change_password():
            self.keystore.update_password(old_pw, new_pw)
            self.save_keystore()
        self.storage.set_password(new_pw, encrypt)
        self.storage.write()

    def save_keystore(self):
        self.storage.put('keystore', self.keystore.dump())


class ImportedWalletBase(Simple_Wallet):

    txin_type = 'p2pkh'

    def get_txin_type(self, address):
        return self.txin_type

    def can_delete_address(self):
        return True

    def has_seed(self):
        return False

    def is_deterministic(self):
        return False

    def is_change(self, address):
        return False

    def get_master_public_keys(self):
        return []

    def is_beyond_limit(self, address, is_change):
        return False

    def get_fingerprint(self):
        return ''

    def get_receiving_addresses(self):
        return self.get_addresses()

    def get_change_addresses(self):
        return []

    def delete_address(self, address):
        assert isinstance(address, Address)
        if address not in self.get_addresses():
            return

        transactions_to_remove = set()  # only referred to by this address
        transactions_new = set()  # txs that are not only referred to by address
        with self.lock:
            for addr, details in self._history.items():
                if addr == address:
                    for tx_hash, height in details:
                        transactions_to_remove.add(tx_hash)
                        self.tx_addr_hist[tx_hash].discard(address)
                        if not self.tx_addr_hist.get(tx_hash):
                            self.tx_addr_hist.pop(tx_hash, None)
                else:
                    for tx_hash, height in details:
                        transactions_new.add(tx_hash)
            transactions_to_remove -= transactions_new
            self._history.pop(address, None)

            for tx_hash in transactions_to_remove:
                self.remove_transaction(tx_hash)
                self.tx_fees.pop(tx_hash, None)
                self.verified_tx.pop(tx_hash, None)
                self.unverified_tx.pop(tx_hash, None)
                self.transactions.pop(tx_hash, None)
                self._addr_bal_cache.pop(address, None)  # not strictly necessary, above calls also have this side-effect. but here to be safe. :)
                if self.verifier:
                    # TX is now gone. Toss its SPV proof in case we have it
                    # in memory. This allows user to re-add PK again and it
                    # will avoid the situation where the UI says "not verified"
                    # erroneously!
                    self.verifier.remove_spv_proof_for_tx(tx_hash)
                # FIXME: what about pruned_txo?

            self.storage.put('verified_tx3', self.verified_tx)

        self.save_transactions()

        self.set_label(address, None)
        self.remove_payment_request(address, {})
        self.set_frozen_state([address], False)

        self.delete_address_derived(address)
        self.save_addresses()


class ImportedAddressWallet(ImportedWalletBase):
    # Watch-only wallet of imported addresses

    wallet_type = 'imported_addr'

    def __init__(self, storage):
        self._sorted = None
        super().__init__(storage)

    @classmethod
    def from_text(cls, storage, text):
        wallet = cls(storage)
        for address in text.split():
            wallet.import_address(Address.from_string(address))
        return wallet

    def is_watching_only(self):
        return True

    def get_keystores(self):
        return []

    def can_import_privkey(self):
        return False

    def load_keystore(self):
        self.keystore = None

    def save_keystore(self):
        pass

    def load_addresses(self):
        addresses = self.storage.get('addresses', [])
        self.addresses = [Address.from_string(addr) for addr in addresses]

    def save_addresses(self):
        self.storage.put('addresses', [addr.to_storage_string()
                                       for addr in self.addresses])
        self.storage.write()

    def can_change_password(self):
        return False

    def can_import_address(self):
        return True

    def get_addresses(self, include_change=False):
        if not self._sorted:
            self._sorted = sorted(self.addresses,
                                  key=lambda addr: addr.to_ui_string())
        return self._sorted

    def import_address(self, address):
        assert isinstance(address, Address)
        if address in self.addresses:
            return False
        self.addresses.append(address)
        self.save_addresses()
        self.storage.write()
        self.add_address(address)
        self._sorted = None
        return True

    def delete_address_derived(self, address):
        self.addresses.remove(address)
        self._sorted.remove(address)

    def add_input_sig_info(self, txin, address):
        x_pubkey = 'fd' + address.to_script_hex()
        txin['x_pubkeys'] = [x_pubkey]
        txin['signatures'] = [None]

class Slp_ImportedAddressWallet(ImportedAddressWallet):
    # Watch-only wallet of imported addresses

    wallet_type = 'slp_imported_addr'

    def __init__(self, storage):
        self._sorted = None
        storage.put('wallet_type', self.wallet_type)
        super().__init__(storage)

class ImportedPrivkeyWallet(ImportedWalletBase):
    # wallet made of imported private keys

    wallet_type = 'imported_privkey'

    def __init__(self, storage):
        Abstract_Wallet.__init__(self, storage)

    @classmethod
    def from_text(cls, storage, text, password=None):
        wallet = cls(storage)
        storage.put('use_encryption', bool(password))
        for privkey in text.split():
            wallet.import_private_key(privkey, password)
        return wallet

    def is_watching_only(self):
        return False

    def get_keystores(self):
        return [self.keystore]

    def can_import_privkey(self):
        return True

    def load_keystore(self):
        if self.storage.get('keystore'):
            self.keystore = load_keystore(self.storage, 'keystore')
        else:
            self.keystore = Imported_KeyStore({})

    def save_keystore(self):
        self.storage.put('keystore', self.keystore.dump())

    def load_addresses(self):
        pass

    def save_addresses(self):
        pass

    def can_change_password(self):
        return True

    def can_import_address(self):
        return False

    def get_addresses(self, include_change=False):
        return self.keystore.get_addresses()

    def delete_address_derived(self, address):
        self.keystore.remove_address(address)
        self.save_keystore()

    def get_address_index(self, address):
        return self.get_public_key(address)

    def get_public_key(self, address):
        return self.keystore.address_to_pubkey(address)

    def import_private_key(self, sec, pw):
        pubkey = self.keystore.import_privkey(sec, pw)
        self.save_keystore()
        self.storage.write()
        self.add_address(pubkey.address)
        return pubkey.address.to_ui_string()

    def export_private_key(self, address, password):
        '''Returned in WIF format.'''
        pubkey = self.keystore.address_to_pubkey(address)
        return self.keystore.export_private_key(pubkey, password)

    def add_input_sig_info(self, txin, address):
        assert txin['type'] == 'p2pkh'
        pubkey = self.keystore.address_to_pubkey(address)
        txin['num_sig'] = 1
        txin['x_pubkeys'] = [pubkey.to_ui_string()]
        txin['signatures'] = [None]

    def pubkeys_to_address(self, pubkey):
        pubkey = PublicKey.from_string(pubkey)
        if pubkey in self.keystore.keypairs:
            return pubkey.address


class Slp_ImportedPrivkeyWallet(ImportedPrivkeyWallet):
    # wallet made of imported private keys

    wallet_type = 'slp_imported_privkey'

    def __init__(self, storage):
        storage.put('wallet_type', self.wallet_type)
        Abstract_Wallet.__init__(self, storage)


class Deterministic_Wallet(Abstract_Wallet):

    def __init__(self, storage):
        Abstract_Wallet.__init__(self, storage)
        self.gap_limit = storage.get('gap_limit', 20)

    def has_seed(self):
        return self.keystore.has_seed()

    def get_receiving_addresses(self):
        return self.receiving_addresses

    def get_change_addresses(self):
        return self.change_addresses

    def get_seed(self, password):
        return self.keystore.get_seed(password)

    def add_seed(self, seed, pw):
        self.keystore.add_seed(seed, pw)

    def change_gap_limit(self, value):
        '''This method is not called in the code, it is kept for console use'''
        with self.lock:
            if value >= self.gap_limit:
                self.gap_limit = value
                self.storage.put('gap_limit', self.gap_limit)
                return True
            elif value >= self.min_acceptable_gap():
                addresses = self.get_receiving_addresses()
                k = self.num_unused_trailing_addresses(addresses)
                n = len(addresses) - k + value
                self.receiving_addresses = self.receiving_addresses[0:n]
                self.gap_limit = value
                self.storage.put('gap_limit', self.gap_limit)
                self.save_addresses()
                return True
            else:
                return False

    def num_unused_trailing_addresses(self, addresses):
        '''This method isn't called anywhere. Perhaps it is here for console use.
        Can't be sure. -Calin '''
        with self.lock:
            k = 0
            for addr in reversed(addresses):
                if addr in self._history:
                    break
                k = k + 1
            return k

    def min_acceptable_gap(self):
        ''' Caller needs to hold self.lock otherwise bad things may happen. '''
        # fixme: this assumes wallet is synchronized
        n = 0
        nmax = 0
        addresses = self.get_receiving_addresses()
        k = self.num_unused_trailing_addresses(addresses)
        for a in addresses[0:-k]:
            if a in self._history:
                n = 0
            else:
                n += 1
                if n > nmax: nmax = n
        return nmax + 1

    def create_new_address(self, for_change=False):
        for_change = bool(for_change)
        with self.lock:
            addr_list = self.change_addresses if for_change else self.receiving_addresses
            n = len(addr_list)
            x = self.derive_pubkeys(for_change, n)
            address = self.pubkeys_to_address(x)
            addr_list.append(address)
            self.save_addresses()
            self.add_address(address)
            return address

    def synchronize_sequence(self, for_change):

        limit = self.gap_limit_for_change if for_change else self.gap_limit
        while True:
            addresses = self.get_change_addresses() if for_change else self.get_receiving_addresses()
            if len(addresses) < limit:
                self.create_new_address(for_change)
                continue
            if all(map(lambda a: not self.address_is_old(a), addresses[-limit:] )):
                break
            else:
                self.create_new_address(for_change)

    def synchronize(self):
        with self.lock:
            if self.storage.get('auto_maintain_gap', True):
                self.synchronize_sequence(False)
            self.synchronize_sequence(True)

    def is_beyond_limit(self, address, is_change):
        with self.lock:
            if is_change:
                addr_list = self.get_change_addresses()
                limit = self.gap_limit_for_change
            else:
                addr_list = self.get_receiving_addresses()
                limit = self.gap_limit
            idx = addr_list.index(address)
            if idx < limit:
                return False
            for addr in addr_list[-limit:]:
                if addr in self._history:
                    return False
            return True

    def get_master_public_keys(self):
        return [self.get_master_public_key()]

    def get_fingerprint(self):
        return self.get_master_public_key()

    def get_txin_type(self, address):
        return self.txin_type


class Simple_Deterministic_Wallet(Simple_Wallet, Deterministic_Wallet):

    """ Deterministic Wallet with a single pubkey per address """

    def __init__(self, storage):
        Deterministic_Wallet.__init__(self, storage)

    def get_public_key(self, address):
        sequence = self.get_address_index(address)
        pubkey = self.get_pubkey(*sequence)
        return pubkey

    def load_keystore(self):
        self.keystore = load_keystore(self.storage, 'keystore')
        try:
            xtype = bitcoin.xpub_type(self.keystore.xpub)
        except:
            xtype = 'standard'
        self.txin_type = 'p2pkh' if xtype == 'standard' else xtype

    def get_pubkey(self, c, i):
        return self.derive_pubkeys(c, i)

    def get_public_keys(self, address):
        return [self.get_public_key(address)]

    def add_input_sig_info(self, txin, address):
        derivation = self.get_address_index(address)
        x_pubkey = self.keystore.get_xpubkey(*derivation)
        txin['x_pubkeys'] = [x_pubkey]
        txin['signatures'] = [None]
        txin['num_sig'] = 1

    def get_master_public_key(self):
        return self.keystore.get_master_public_key()

    def derive_pubkeys(self, c, i):
        return self.keystore.derive_pubkey(c, i)


class Standard_Wallet(Simple_Deterministic_Wallet):
    wallet_type = 'standard'
    def __init__(self, storage):
        super().__init__(storage)

    def pubkeys_to_address(self, pubkey):
        return Address.from_pubkey(pubkey)


class Slp_Standard_Wallet(Standard_Wallet):
    wallet_type = 'slp_standard'
    def __init__(self, storage):
        storage.put('wallet_type', self.wallet_type)
        super().__init__(storage)


class Multisig_Wallet(Deterministic_Wallet):
    # generic m of n
    gap_limit = 20

    def __init__(self, storage):
        self.wallet_type = storage.get('wallet_type')
        self.m, self.n = multisig_type(self.wallet_type)
        Deterministic_Wallet.__init__(self, storage)

    def get_pubkeys(self, c, i):
        return self.derive_pubkeys(c, i)

    def pubkeys_to_address(self, pubkeys):
        pubkeys = [bytes.fromhex(pubkey) for pubkey in pubkeys]
        redeem_script = self.pubkeys_to_redeem_script(pubkeys)
        return Address.from_multisig_script(redeem_script)

    def pubkeys_to_redeem_script(self, pubkeys):
        return Script.multisig_script(self.m, sorted(pubkeys))

    def derive_pubkeys(self, c, i):
        return [k.derive_pubkey(c, i) for k in self.get_keystores()]

    def load_keystore(self):
        self.keystores = {}
        for i in range(self.n):
            name = 'x%d/'%(i+1)
            self.keystores[name] = load_keystore(self.storage, name)
        self.keystore = self.keystores['x1/']
        xtype = bitcoin.xpub_type(self.keystore.xpub)
        self.txin_type = 'p2sh' if xtype == 'standard' else xtype

    def save_keystore(self):
        for name, k in self.keystores.items():
            self.storage.put(name, k.dump())

    def get_keystore(self):
        return self.keystores.get('x1/')

    def get_keystores(self):
        return [self.keystores[i] for i in sorted(self.keystores.keys())]

    def update_password(self, old_pw, new_pw, encrypt=False):
        if old_pw is None and self.has_password():
            raise InvalidPassword()
        for name, keystore in self.keystores.items():
            if keystore.can_change_password():
                keystore.update_password(old_pw, new_pw)
                self.storage.put(name, keystore.dump())
        self.storage.set_password(new_pw, encrypt)
        self.storage.write()

    def has_seed(self):
        return self.keystore.has_seed()

    def can_change_password(self):
        return self.keystore.can_change_password()

    def is_watching_only(self):
        return not any([not k.is_watching_only() for k in self.get_keystores()])

    def get_master_public_key(self):
        return self.keystore.get_master_public_key()

    def get_master_public_keys(self):
        return [k.get_master_public_key() for k in self.get_keystores()]

    def get_fingerprint(self):
        return ''.join(sorted(self.get_master_public_keys()))

    def add_input_sig_info(self, txin, address):
        # x_pubkeys are not sorted here because it would be too slow
        # they are sorted in transaction.get_sorted_pubkeys
        derivation = self.get_address_index(address)
        txin['x_pubkeys'] = [k.get_xpubkey(*derivation) for k in self.get_keystores()]
        txin['pubkeys'] = None
        # we need n place holders
        txin['signatures'] = [None] * self.n
        txin['num_sig'] = self.m

    def is_multisig(self):
        return True


wallet_types = ['standard', 'slp_standard', 'multisig', 'slp_multisig', 'imported', 'slp_imported']

def register_wallet_type(category):
    wallet_types.append(category)

wallet_constructors = {
    'standard': Standard_Wallet,
    'slp_standard': Slp_Standard_Wallet,
    'old': Standard_Wallet,
    'xpub': Standard_Wallet,
    'imported_privkey': ImportedPrivkeyWallet,
    'slp_imported_privkey': Slp_ImportedPrivkeyWallet,
    'imported_addr': ImportedAddressWallet,
    'slp_imported_addr': Slp_ImportedAddressWallet,
}

def register_constructor(wallet_type, constructor):
    wallet_constructors[wallet_type] = constructor

class UnknownWalletType(RuntimeError):
    ''' Raised if encountering an unknown wallet type '''
    pass

# former WalletFactory
class Wallet(object):
    """The main wallet "entry point".
    This class is actually a factory that will return a wallet of the correct
    type when passed a WalletStorage instance."""

    def __new__(self, storage):
        # Convert 'bip39-slp' wallet type to 'slp_standard' wallet type
        if storage.get('wallet_type', '') == 'bip39-slp' or storage.get('wallet_type', '') == 'standard_slp':
            storage.put('wallet_type', 'slp_standard')
        wallet_type = storage.get('wallet_type')
        WalletClass = Wallet.wallet_class(wallet_type)
        wallet = WalletClass(storage)
        # Convert hardware wallets restored with older versions of
        # Electrum to BIP44 wallets.  A hardware wallet does not have
        # a seed and plugins do not need to handle having one.
        rwc = getattr(wallet, 'restore_wallet_class', None)
        if rwc and storage.get('seed', ''):
            storage.print_error("converting wallet type to " + rwc.wallet_type)
            storage.put('wallet_type', rwc.wallet_type)
            wallet = rwc(storage)
        return wallet

    @staticmethod
    def wallet_class(wallet_type):
        if multisig_type(wallet_type):
            return Multisig_Wallet
        if wallet_type in wallet_constructors:
            return wallet_constructors[wallet_type]
        raise UnknownWalletType("Unknown wallet type: " + str(wallet_type))
