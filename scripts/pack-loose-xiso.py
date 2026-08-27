#!/usr/bin/env python3
"""
pack-loose-xiso.py — build an XDVDFS image that boots with NO ROM on the disc.

The disc carries default.xbe plus the extracted loose-file tree produced by
tools/extract-loose:

    files/...          2011 file-table entries (incl. the bgdata/ subdirectory)
    segs/...           26 segments
    filenames.lst      file-number -> name table

port/src/romdata.c already knows how to run from these: romdataInit() falls
back to filenames.lst when no ROM is present, romdataFileLoad() looks in
files/<name>, and romdataInitSegment() looks in segs/<name>. Keeping the ROM
off the disc frees the 32 MB it would otherwise occupy in RAM.

Usage:
    python scripts/pack-loose-xiso.py --xbe build-xbox/default.xbe \\
        --loose build/loose --out build-xbox/perfectdarkx-loose.iso
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import xdvdfs_tree
from xdvdfs_tree import pack_xiso


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--xbe', required=True, help='default.xbe to boot')
    ap.add_argument('--loose', required=True,
                    help='directory holding files/, segs/ and filenames.lst')
    ap.add_argument('--out', default='perfectdarkx-loose.iso')
    ap.add_argument('--gbc', default=None, help='optional pd.gbc')
    ap.add_argument('--boot-ini', default=None,
                    help='optional pdx_boot.ini for a qualification-config XBE')
    ap.add_argument('--texture-pack', default=None,
                    help='optional ext_tex.pak built by tools/texturepack/build_xbox_pack.py')
    args = ap.parse_args()

    if not os.path.isfile(args.xbe):
        sys.exit('XBE not found: %s' % args.xbe)
    if not os.path.isdir(args.loose):
        sys.exit('loose dir not found: %s' % args.loose)

    entries = [('default.xbe', args.xbe)]

    controlled_root_files = {'pd.ini', 'pdx_boot.ini'}
    tree = [entry for entry in xdvdfs_tree.collect_tree(args.loose)
            if entry[0].replace('\\', '/').lower() not in controlled_root_files]
    if not tree:
        sys.exit('no files found under %s' % args.loose)
    entries.extend(tree)
    # Never leak host-generated configuration files into an image. Xbox
    # release settings are fixed in the XBE; pdx_boot.ini is accepted only for
    # an explicitly compiled qualification build.
    if args.boot_ini:
        if not os.path.isfile(args.boot_ini):
            sys.exit('boot ini override not found: %s' % args.boot_ini)
        entries.append(('pdx_boot.ini', args.boot_ini))

    if args.gbc and os.path.isfile(args.gbc):
        entries.append(('pd.gbc', args.gbc))

    if args.texture_pack:
        if not os.path.isfile(args.texture_pack):
            sys.exit('texture pack not found: %s' % args.texture_pack)
        entries.append(('ext_tex.pak', args.texture_pack))

    # Sanity-check the pieces romdata.c needs before burning an image.
    names = set(n for n, _ in entries)
    if 'filenames.lst' not in names:
        sys.exit('filenames.lst missing from %s -- the no-ROM path needs it' % args.loose)
    if not any(n.startswith('segs/') for n in names):
        sys.exit('no segs/ entries -- romdataInitSegment would fall back to a ROM')
    if not any(n.startswith('files/') for n in names):
        sys.exit('no files/ entries')

    print('XBE   : %s' % args.xbe)
    print('loose : %s (%d entries)' % (args.loose, len(tree)))
    xdvdfs_tree.build(args.out, entries)


if __name__ == '__main__':
    main()
