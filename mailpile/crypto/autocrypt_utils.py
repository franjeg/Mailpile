# Copyright (C) 2018 Jack Dodds
# This code is part of Mailpile and is hereby released under the
# Gnu Affero Public Licence v.3 - see ../../COPYING and ../../AGPLv3.txt.

import base64
import datetime
import pgpdump
import time


##[ Begin borrowed code ... ]################################################
#
# Based on:
#
# https://github.com/mailencrypt/inbome/blob/master/src/inbome/parse.py

def parse_autocrypt_headervalue(value, optional_attrs=None):
    """
    Parse an AutoCrypt header. Will return an empty dict if parsing fails.

    Optional attributes may be added to the result dictionary, but only the ones
    listed in optional_attrs (a list or dict); others are ignored.
    """
    result_dict = {}
    try:
        for x in value.split(";"):
            kv = x.split("=", 1)
            name = kv[0].strip()
            value = kv[1].strip()
            if name in ("addr", "prefer-encrypted"):
                result_dict["addr"] = value
            elif name == "keydata":
                keydata_base64 = "".join(value.split())
                keydata = base64.b64decode(keydata_base64)
                result_dict[name] = keydata
            elif name[:1] == '_':
                if optional_attrs and name in optional_attrs:
                    result_dict["addr"] = value
            else:
                # Unknown value detected, refuse to parse any further
                return {}
    except (ValueError, TypeError, IndexError):
        return {}

    if "keydata" not in result_dict:
        # found no keydata, ignoring header
        return {}

    if "addr" not in result_dict:
        # found no e-mail address, ignoring header
        return {}

    if result_dict.get("prefer-encrypted") not in ("mutual", None):
        # Invalid prefer-encrypted value
        return {}

    return result_dict


def extract_autocrypt_header(msg, to=None, optional_attrs=None):
    all_results = []
    for inb in (msg.get_all("Autocrypt") or []):
        res = parse_autocrypt_headervalue(inb, optional_attrs=optional_attrs)
        if res and (not to or res['addr'] == to):
            all_results.append(res)

    # Return parsed header iff we found exactly one.
    if len(all_results) == 1:
        return all_results[0]
    else:
        return {}

    # FIXME: The AutoCrypt spec talks about synthesizing headers from other
    #        details. That would make sense if AutoCrypt was our primary
    # mechanism, but we're not really there yet. Needs more thought.


def extract_autocrypt_gossip_headers(msg, to=None, optional_attrs=None):
    all_results = []
    for inb in (msg.get_all("Autocrypt-Gossip") or []):
        res = parse_autocrypt_headervalue(inb, optional_attrs=optional_attrs)
        if res and (not to or res['addr'] == to):
            all_results.append(res)

    return all_results


def get_minimal_PGP_key(keydata,
                        user_id=None, subkey_id=None, binary_out=False):
    """
    Accepts a PGP key (armored or binary) and returns a minimal PGP key
    containing exactly five packets (base64 or binary) defining a
    primary key, a single user id with one self-signature, and a
    single encryption subkey with one self-signature. Such a five packet
    key MUST be used in Autocrypt headers (Level 1 Spec section 2.1.1).
    The unrevoked user id with newest unexpired self-signature and the
    unrevoked encryption-capable subkey with newest unexpired
    self-signature are selected from the input key.
    If user_id is provided, a user id containing that string will be
    selected if there is one, otherwise any user id will be accepted.
    If subkey_id is specified, only a subkey with that id will be selected.

    Along with the new key, the selected user id and subkey id are returned.
    Returns None if there is a failure.
    """
    def _get_int4(data, offset):
        '''Pull four bytes from data at offset and return as an integer.'''
        return ((data[offset] << 24) + (data[offset + 1] << 16) +
                (data[offset + 2] << 8) + data[offset + 3])

    def _exp_time(creation_time, exp_time_subpacket_data):

        life_s = _get_int4(exp_time_subpacket_data, 0)
        if not life_s:
            return 0
        return packet.creation_time + datetime.timedelta( seconds = life_s)

    def _pgp_header(type, body_length):

        if body_length < 192:
            return bytearray([type+0xC0, body_length])
        elif body_length < 8384:
            return bytearray([type+0xC0, (body_length-192)//256+192,
                                                 (body_length-192)%256])
        else:
            return bytearray([type+0xC0, 255,
                    body_length//(1<<24), body_length//(1<<16) % 256,
                    body_length//1<<8 % 256, body_length % 256])

    pri_key = None
    u_id = None
    u_id_sig = None
    u_id_match = False
    s_key = None
    s_key_sig = None
    now = datetime.datetime.utcfromtimestamp(time.time())

    if '-----BEGIN PGP PUBLIC KEY BLOCK-----' in keydata:
        packet_iter = pgpdump.AsciiData(keydata).packets()
    else:
        packet_iter = pgpdump.BinaryData(keydata).packets()

    try:
        packet = next(packet_iter)
    except:
        packet = None

    while packet:

        if packet.raw == 6 and pri_key:     # Primary key must be the first
            break                           # and only the first packet.
        elif packet.raw != 6 and not pri_key:
            break

        elif packet.raw == 6:               # Primary Public-Key Packet
            pri_key = packet

        elif packet.raw == 13:              # User ID Packet
            u_id_try = packet
            u_id_sig_try = None
            u_id_try_match = not user_id or user_id in u_id_try.user
            #**FIXME Autocrypt spec 5.1 requires E-mail address canonicalization

            # Accept a nonmatching u_id IFF no other u_id matches.
            if u_id_match and not u_id_try_match:
                u_id_try = None

            for packet in packet_iter:
                if packet.raw != 2:         # Signature Packet
                    break
                elif not u_id_try:
                    continue
                                            # User ID certification
                elif packet.raw_sig_type in (0x10, 0x11, 0x12, 0x13, 0x1F):
                    if (packet.key_id in pri_key.fingerprint and
                            (not packet.expiration_time or
                                packet.expiration_time > now) and
                            (not u_id_sig_try or
                                u_id_sig_try.creation_time
                                    < packet.creation_time)):
                        u_id_sig_try = packet
                                            # Certification revocation
                elif packet.raw_sig_type == 0x30:
                    if packet.key_id in pri_key.fingerprint:
                        u_id_try = None
                        u_id_sig_try = None

            # Select unrevoked user id with newest unexpired self-signature
            if u_id_try and u_id_sig_try and (
                    not u_id or not u_id_sig or
                    u_id_try_match and not u_id_match or
                    u_id_sig_try.creation_time >= u_id_sig.creation_time):
                u_id = u_id_try
                u_id_sig = u_id_sig_try
                u_id_match = u_id_try_match
            continue    # Skip next(packet_iter) - for has done it.

        elif packet.raw == 14:              # Public-Subkey Packet
            s_key_try = packet
            s_key_sig_try = None

            # Honour a request for specific subkey and check for expiry.
            if ((subkey_id and not s_key_try.fingerprint.endswith(subkey_id))
                    or (s_key_try.expiration_time and
                        s_key_try.expiration_time < now)):
                s_key_try = None

            for packet in packet_iter:
                if packet.raw != 2:         # Signature Packet
                    break
                elif not s_key_try:
                    continue
                                            # Subkey Binding Signature
                elif packet.raw_sig_type == 0x18:
                    packet.key_expire_time = None
                    if (packet.key_id in pri_key.fingerprint and
                            not packet.expiration_time or
                            packet.expiration_time >= now):
                        can_encrypt = True  # Assume encrypt if no flags.
                        for subpacket in packet.subpackets:
                            if subpacket.subtype == 9:  # Key expiration
                                # pgpdump should provide this!!
                                packet.key_expire_time = _exp_time(
                                    packet.creation_time, subpacket.data)
                            elif subpacket.subtype == 27:   # Key flags
                                can_encrypt |= subpacket.data[0] & 0x0C
                        if can_encrypt and (not packet.key_expire_time or
                                            packet.key_expire_time >= now):
                            s_key_sig_try = packet
                                            # Subkey revocation signature
                elif packet.raw_sig_type == 0x28:
                    if packet.key_id in pri_key.fingerprint:
                        s_key_try = None
                        s_key_sig_try = None

            # Select unrevoked encryption-capable subkey with newest
            # unexpired self-signature (ignores newness of key itself).
            if s_key_try and s_key_sig_try and (not s_key_sig or
                    s_key_sig_try.creation_time >= s_key_sig.creation_time):
                s_key = s_key_try
                s_key_sig = s_key_sig_try
            continue    # Skip next(packet_iter) - for has done it.

        try:
            packet = next(packet_iter)
        except:
            packet = None

    if not(pri_key and u_id and u_id_sig and s_key and s_key_sig):
        return '', None, None

    newkey = (
        _pgp_header(pri_key.raw, len(pri_key.data)) + pri_key.data +
        _pgp_header(u_id.raw, len(u_id.data)) + u_id.data +
        _pgp_header(u_id_sig.raw, len(u_id_sig.data)) + u_id_sig.data +
        _pgp_header(s_key.raw, len(s_key.data)) + s_key.data +
        _pgp_header(s_key_sig.raw, len(s_key_sig.data)) + s_key_sig.data )

    if not binary_out:
        newkey = base64.b64encode(newkey)

    return newkey, u_id.user, s_key.key_id


if __name__ == "__main__":

    import os

    default_file = os.path.dirname(os.path.abspath(__file__))
    default_file = os.path.abspath(default_file + '/../tests/data/pub.key')

    print
    print 'Default key file:', default_file
    print

    key_file_path = raw_input('Enter key file path or <Enter> for default: ')
    if key_file_path == '':
        key_file_path = default_file

    user_id = raw_input('Enter email address: ')
    if user_id == '':
        user_id = None

    subkey_id = raw_input('Enter subkey_id: ')
    if subkey_id == '':
        subkey_id = None

    with open(key_file_path, 'r') as keyfile:
        keydata = bytearray( keyfile.read() )

    print 'Key length:', len(keydata)

    newkey, u, i = get_minimal_PGP_key(
        keydata, user_id=user_id, subkey_id=subkey_id, binary_out=True)

    print 'User ID:', u
    print 'Subkey ID:', i
    print 'Minimal key length:', len(newkey)
    key_file_path += '.min.gpg'
    print 'Minimal key output file:', key_file_path

    with open(key_file_path, 'w') as keyfile:
        keyfile.write(newkey)

    quit()
