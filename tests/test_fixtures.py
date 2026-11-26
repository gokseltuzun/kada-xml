"""Regression test on real Premiere exports ("torture" sequences, tests/fixtures).

Each fixture is converted and checked for the things that broke Final Cut Pro before:
  - every V1 clip with media is in the spine (none dropped next to a transition)
  - V1 transitions are carried, stills are <video> (FCP crashes on a still in <asset-clip>)
  - clip starts sit on their media's frame grid, connected clips on the sequence grid
    (otherwise FCP warns "not on an edit frame boundary")
  - the output is valid against Final Cut's own DTD, when Final Cut is installed
Run: python tests/test_fixtures.py
"""
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from fractions import Fraction as F

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import premiere_to_fcpxml as core  # noqa: E402

FIX = os.path.join(HERE, 'fixtures')
DTD = ('/Applications/Final Cut Pro.app/Contents/Frameworks/Interchange.framework/'
       'Versions/A/Resources/FCPXMLv1_10.dtd')
STILL = core.STILL_EXT


def t(v):
    v = v[:-1]
    if '/' in v:
        a, b = v.split('/')
        return F(int(a), int(b))
    return F(v)


def on(v, g):
    return g is None or (v / g).denominator == 1


def check(src):
    errs = []
    with tempfile.TemporaryDirectory() as d:
        dst = os.path.join(d, 'out.fcpxml')
        core.convert(src, dst)
        prem = ET.parse(src).getroot().find('sequence')
        out = ET.parse(dst).getroot()

        # 1. nothing on V1 dropped
        v1 = prem.find('media/video').findall('track')[0]
        media_ids = {f.get('id') for f in ET.parse(src).getroot().iter('file')
                     if f.findtext('pathurl')}
        want = sum(1 for c in v1.findall('clipitem')
                   if c.find('file') is not None and c.find('file').get('id') in media_ids)
        spine = out.find('.//spine')
        got = sum(1 for c in spine if c.tag in ('asset-clip', 'video'))
        if got != want:
            errs.append(f'V1 clips: {got} in spine, {want} in Premiere')

        # 2. transitions carried, stills as <video>
        want_tr = len(v1.findall('transitionitem'))
        got_tr = len(spine.findall('transition'))
        if got_tr != want_tr:
            errs.append(f'transitions: {got_tr} carried, {want_tr} in Premiere')
        assets = {a.get('id'): a for a in out.iter('asset')}
        for c in out.iter('asset-clip'):
            src_ = assets[c.get('ref')].find('media-rep').get('src').lower()
            if src_.endswith(STILL):
                errs.append(f'still inside asset-clip: {c.get("name")}')

        # 3. frame grids
        fmts = {f.get('id'): (t(f.get('frameDuration')) if f.get('frameDuration') else None)
                for f in out.iter('format')}
        sfd = fmts[out.find('.//sequence').get('format')]

        def grid(el):
            a = assets.get(el.get('ref'))
            if el.tag != 'asset-clip' or el.find('timeMap') is not None or a is None \
                    or a.get('hasVideo') != '1':
                return None
            return fmts.get(a.get('format'))

        for p in spine:
            if p.tag in ('asset-clip', 'video', 'gap'):
                if not on(t(p.get('offset')), sfd) or not on(t(p.get('duration')), sfd):
                    errs.append(f'spine item off the sequence grid: {p.get("name")}')
                if p.tag == 'asset-clip' and not on(t(p.get('start')), grid(p)):
                    errs.append(f'start off the media grid: {p.get("name")} {p.get("start")}')
                for k in p:
                    if k.tag in ('asset-clip', 'video', 'marker'):
                        key = 'start' if k.tag == 'marker' else 'offset'
                        rel = t(k.get(key)) - t(p.get('start') or '0s')
                        if not on(rel, sfd):
                            errs.append(f'connected item off the grid: {k.get("name") or k.get("value")}')

        # 4. Final Cut's own DTD
        if os.path.exists(DTD):
            dtd = os.path.join(d, 'fcpxml.dtd')
            with open(DTD, 'rb') as a, open(dtd, 'wb') as b:
                b.write(a.read())
            r = subprocess.run(['xmllint', '--noout', '--dtdvalid', dtd, dst],
                               capture_output=True, text=True)
            if r.returncode:
                errs.append('DTD: ' + r.stderr.strip()[:300])
    return errs


def main():
    bad = 0
    for name in sorted(os.listdir(FIX)):
        if not name.endswith('.xml'):
            continue
        errs = check(os.path.join(FIX, name))
        print(('OK   ' if not errs else 'FAIL ') + name)
        for e in errs:
            print('     ' + e)
        bad += bool(errs)
    print('ALL OK' if not bad else f'{bad} FAILED')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
