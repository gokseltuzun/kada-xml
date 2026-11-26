#!/usr/bin/env python3
"""KADA XML - Premiere Pro "Final Cut Pro XML" (FCP7 xmeml) -> Final Cut Pro X FCPXML 1.10.

Command line:
  python premiere_to_fcpxml.py INPUT.xml OUTPUT.fcpxml [--from "E:/Media"] [--to "/Volumes/Disk/Media"]

What carries over: cuts, in/out points, track layout, gaps, clip enable state,
constant audio levels (dB), constant speed (check it).
What does not: effects, color, transitions, titles/generators, keyframes (they are reported).

Layout in FCPX: V1 = primary storyline, V2.. = lanes above, audio tracks = lanes below.
Video clips are video-only (srcEnable=video), audio comes as separate clips, as in Premiere.
Stereo audio that Premiere splits into L/R tracks becomes one clip.
For WAV files, the BWF time reference is read from the file when it can be found.

No dependencies beyond the Python standard library.
"""
import sys
import os
import argparse
import math
import struct
from collections import Counter
from fractions import Fraction
from urllib.parse import unquote, quote
import xml.etree.ElementTree as ET
from xml.sax.saxutils import quoteattr

__version__ = "1.0.2"


class ConvertError(Exception):
    pass


def _t(fr):
    fr = Fraction(fr)
    if fr.denominator == 1:
        return f"{fr.numerator}s"
    return f"{fr.numerator}/{fr.denominator}s"


def _rate_of(el, default=None):
    r = el.find('rate')
    if r is None or r.findtext('timebase') is None:
        return default
    tb = int(r.findtext('timebase'))
    ntsc = (r.findtext('ntsc') or 'FALSE').upper() == 'TRUE'
    return Fraction(tb * 1000, 1001) if ntsc else Fraction(tb)


def _pathurl_to_local(u):
    u = unquote(u)
    for p in ('file://localhost/', 'file:///', 'file://'):
        if u.startswith(p):
            u = u[len(p):]
            break
    u = u.replace('\\', '/')
    # "E:/x" stays as is; "Volumes/x" (mac, lost leading slash) gets it back
    if not (len(u) > 1 and u[1] == ':') and not u.startswith('/'):
        u = '/' + u
    return u


def _qt_has_tmcd(path):
    """QuickTime/MP4: does the file carry a timecode (tmcd) track that FCPX reads?
    True / False, or None when the file can't be read."""
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as fh:
            pos = 0
            while pos + 8 <= size:
                fh.seek(pos)
                hdr = fh.read(8)
                if len(hdr) < 8:
                    break
                n, kind = struct.unpack('>I4s', hdr)
                hl = 8
                if n == 1:
                    n = struct.unpack('>Q', fh.read(8))[0]
                    hl = 16
                elif n == 0:
                    n = size - pos
                if kind == b'moov':
                    if n > 200 * 1024 * 1024:
                        return None
                    return b'tmcd' in fh.read(n - hl)
                if n < hl:
                    break
                pos += n
    except OSError:
        return None
    return None


def _bwf_time_reference(path):
    """WAV bext chunk -> (time_reference_samples, samplerate) or None."""
    try:
        with open(path, 'rb') as f:
            if f.read(4) != b'RIFF':
                return None
            f.read(4)
            if f.read(4) != b'WAVE':
                return None
            sr = tref = None
            while True:
                hdr = f.read(8)
                if len(hdr) < 8:
                    break
                cid, size = hdr[:4], struct.unpack('<I', hdr[4:])[0]
                if cid == b'fmt ':
                    d = f.read(size)
                    sr = struct.unpack('<I', d[4:8])[0]
                elif cid == b'bext' and size >= 346:
                    d = f.read(size)
                    lo, hi = struct.unpack('<II', d[338:346])
                    tref = lo + (hi << 32)
                else:
                    f.seek(size, 1)
                if size % 2:
                    f.seek(1, 1)
                if sr is not None and tref is not None:
                    return tref, sr
    except (OSError, struct.error):
        return None
    return None


def _load(src):
    try:
        root = ET.parse(src).getroot()
    except ET.ParseError as e:
        raise ConvertError(f"XML okunamadi / could not read XML: {e}")
    seq = root.find('.//sequence')
    if root.tag != 'xmeml' or seq is None:
        raise ConvertError("Bu bir Premiere 'Final Cut Pro XML' dosyasi degil / "
                           "not a Premiere 'Final Cut Pro XML' (xmeml) file")
    return root, seq


def media_paths(src):
    root, _ = _load(src)
    out = []
    for f in root.iter('file'):
        if f.find('pathurl') is not None:
            out.append(_pathurl_to_local(f.findtext('pathurl')))
    return out


def detect_media_root(src):
    """Common folder of all media files in the XML (for the path mapping field)."""
    paths = media_paths(src)
    if not paths:
        return ''
    parts = [p.split('/') for p in paths]
    common = []
    for segs in zip(*[p[:-1] for p in parts]):
        if all(s.lower() == segs[0].lower() for s in segs):
            common.append(segs[0])
        else:
            break
    return '/'.join(common)


def _map_path(p, frm, to):
    if frm and to:
        f = frm.replace('\\', '/').rstrip('/')
        if p.lower().startswith(f.lower()):
            return to.replace('\\', '/').rstrip('/') + p[len(f):]
    return p


def convert(src, dst, frm=None, to=None):
    """Convert. Returns a dict report. Raises ConvertError."""
    root, seq = _load(src)
    seq_name = seq.findtext('name') or 'Sequence'
    fps = _rate_of(seq)
    if fps is None:
        raise ConvertError("Sekans frame rate bulunamadi / sequence frame rate missing")
    fd = 1 / fps
    vfmt = seq.find('media/video/format/samplecharacteristics')
    W = int(vfmt.findtext('width')) if vfmt is not None and vfmt.findtext('width') else 1920
    H = int(vfmt.findtext('height')) if vfmt is not None and vfmt.findtext('height') else 1080
    warnings = Counter()

    # ---- assets ----
    files = {}
    for f in root.iter('file'):
        if f.find('pathurl') is None:
            continue
        fid = f.get('id')
        if fid in files:
            continue
        local = _pathurl_to_local(f.findtext('pathurl'))
        mapped = _map_path(local, frm, to)
        frate = _rate_of(f, fps)
        dur = Fraction(int(f.findtext('duration') or 0)) / frate
        has_v = f.find('media/video') is not None
        has_a = f.find('media/audio') is not None
        ch = int(f.findtext('media/audio/channelcount') or 2) if has_a else 0
        sr = int(f.findtext('media/audio/samplecharacteristics/samplerate') or 48000) if has_a else 48000
        start = Fraction(0)
        tc = f.find('timecode')
        if tc is not None and tc.findtext('frame'):
            start = Fraction(int(tc.findtext('frame'))) / _rate_of(tc, frate)
        if not has_v:
            bw = _bwf_time_reference(local) or _bwf_time_reference(mapped)
            if bw:
                start = Fraction(bw[0], bw[1])
                sr = bw[1]
            else:
                start = Fraction(round(start * sr), sr)
                if local.lower().endswith(('.wav', '.bwf')):
                    warnings['WAV dosyasi bulunamadi, timecode XML\'den alindi / '
                             'WAV not found, timecode taken from XML (check sync)'] += 1
        else:
            # FCPX takes a QuickTime/MP4 file's start from its tmcd track only.
            # Sony XAVC .MP4 keeps timecode elsewhere (rtmd/XML), so FCPX starts it at 0.
            tm = _qt_has_tmcd(mapped)
            if tm is None:
                tm = _qt_has_tmcd(local)
            if tm is None and local.lower().endswith(('.mp4', '.m4v')):
                tm = False
                warnings['MP4 bulunamadi, timecode 0 kabul edildi / '
                         'MP4 not found, start timecode assumed 0'] += 1
            if tm is False:
                start = Fraction(0)
            start = Fraction(round(start * fps)) / fps
        out_path = mapped if mapped.startswith('/') else '/' + mapped
        while '//' in out_path:
            out_path = out_path.replace('//', '/')
        parent = os.path.basename(os.path.dirname(local))
        name = f.findtext('name') or os.path.basename(local)
        files[fid] = dict(id=f"r{len(files) + 2}", name=name, parent=parent, path=out_path,
                          start=start, dur=dur, has_v=has_v, has_a=has_a, ch=ch, sr=sr)

    cnt = Counter(v['name'] for v in files.values())
    for v in files.values():
        v['label'] = f"{v['parent']}_{v['name']}" if cnt[v['name']] > 1 else v['name']

    # ---- clips ----
    def clip_info(ci, kind, track_no):
        fe = ci.find('file')
        if fe is None or fe.get('id') not in files:
            warnings['dosyasiz klip atlandi / clip without media skipped'] += 1
            return None
        start = int(ci.findtext('start'))
        end = int(ci.findtext('end'))
        if start < 0 or end < 0:
            warnings['gecisli klip atlandi / clip inside a transition skipped'] += 1
            return None
        r = _rate_of(ci, fps)
        cin = Fraction(int(ci.findtext('in'))) / r
        speed = 1.0
        vol_db = None
        for flt in ci.findall('filter'):
            eid = flt.findtext('effect/effectid') or ''
            ename = flt.findtext('effect/name') or eid
            if eid == 'timeremap':
                for p in flt.iter('parameter'):
                    if p.findtext('parameterid') == 'speed':
                        speed = float(p.findtext('value')) / 100.0
                        if p.findall('keyframe'):
                            warnings['degisken hiz sabit alindi / variable speed made constant'] += 1
            elif eid == 'audiolevels':
                for p in flt.iter('parameter'):
                    if p.findtext('parameterid') == 'level':
                        lv = float(p.findtext('value'))
                        if p.findall('keyframe'):
                            warnings['ses keyframe tasinmadi / audio keyframes not carried'] += 1
                        if abs(lv - 1.0) > 1e-4:
                            vol_db = 20 * math.log10(lv) if lv > 0 else -96
            elif eid in ('basic', 'opacity', 'basicmotion', 'audiopan'):
                pass
            else:
                warnings[f'efekt tasinmadi / effect not carried: {ename}'] += 1
        enabled = (ci.findtext('enabled') or 'TRUE').upper() == 'TRUE'
        return dict(fid=fe.get('id'), off=Fraction(start) / fps,
                    dur=Fraction(end - start) / fps, cin=cin, speed=speed,
                    vol=vol_db, enabled=enabled, kind=kind, track=track_no)

    vnode = seq.find('media/video')
    anode = seq.find('media/audio')
    vtracks = vnode.findall('track') if vnode is not None else []
    atracks = anode.findall('track') if anode is not None else []
    vclips, aclips = [], []
    for i, tr in enumerate(vtracks):
        for ci in tr.findall('clipitem'):
            c = clip_info(ci, 'video', i)
            if c:
                vclips.append(c)
        n = len(tr.findall('generatoritem'))
        if n:
            warnings['yazi/generator tasinmadi / titles & generators not carried'] += n
    n = len(list(seq.iter('transitionitem')))
    if n:
        warnings['gecis tasinmadi / transitions not carried'] += n

    lane_no = 0
    for tr in atracks:
        if int(tr.get('currentExplodedTrackIndex', '0')) > 0:
            continue  # right channel of a split stereo track
        lane_no += 1
        for ci in tr.findall('clipitem'):
            c = clip_info(ci, 'audio', lane_no)
            if c:
                aclips.append(c)

    total = Fraction(int(seq.findtext('duration') or 0)) / fps
    ends = [c['off'] + c['dur'] for c in vclips + aclips]
    if ends and max(ends) > total:
        total = max(ends)

    # ---- primary storyline = V1 ----
    v1 = sorted([c for c in vclips if c['track'] == 0], key=lambda c: c['off'])
    def spd(c):
        return Fraction(c['speed']).limit_denominator(10000)

    def retimed(c):
        return c['speed'] > 0 and abs(c['speed'] - 1.0) > 1e-6

    def src_start(c):
        # Premiere: on a speed-changed clip in/out are in retimed frames -> convert to source time
        return files[c['fid']]['start'] + (c['cin'] * spd(c) if retimed(c) else c['cin'])

    def local_start(c):
        # FCPX: with a timeMap, the clip's start is in retimed (local) time
        return src_start(c) / spd(c) if retimed(c) else src_start(c)

    spine = []
    cur = Fraction(0)
    for c in v1:
        if c['off'] < cur:
            warnings['V1 ust uste binen klip / overlapping V1 clip'] += 1
            continue
        if c['off'] > cur:
            spine.append(dict(gap=True, off=cur, dur=c['off'] - cur, lstart=Fraction(0)))
        spine.append(dict(gap=False, clip=c, off=c['off'], dur=c['dur'],
                          lstart=local_start(c)))
        cur = c['off'] + c['dur']
    if total > cur or not spine:
        spine.append(dict(gap=True, off=cur, dur=max(total - cur, fd), lstart=Fraction(0)))
    for s in spine:
        s['children'] = []

    def parent_for(off):
        for s in spine:
            if s['off'] <= off < s['off'] + s['dur']:
                return s
        return spine[-1]

    for c in [c for c in vclips if c['track'] > 0] + aclips:
        parent_for(c['off'])['children'].append(c)

    def clip_xml(c, indent, lane=None, parent=None, children=()):
        fa = files[c['fid']]
        src_en = None
        if c['kind'] == 'video' and fa['has_a']:
            src_en = 'video'
        if c['kind'] == 'audio' and fa['has_v']:
            src_en = 'audio'
        off = c['off'] if parent is None else parent['lstart'] + (c['off'] - parent['off'])
        start = local_start(c)
        attrs = [f'ref="{fa["id"]}"', f'name={quoteattr(fa["label"])}', f'offset="{_t(off)}"',
                 f'start="{_t(start)}"', f'duration="{_t(c["dur"])}"']
        if lane is not None:
            attrs.append(f'lane="{lane}"')
        if src_en:
            attrs.append(f'srcEnable="{src_en}"')
        if fa['has_v']:
            attrs += ['format="r1"', 'tcFormat="NDF"']
        if not c['enabled']:
            attrs.append('enabled="0"')
        inner = []
        if retimed(c):
            sp = spd(c)
            # linear map 0 -> 0 that covers the clip's whole local range
            inner.append(f'{indent}  <timeMap><timept time="0s" value="0s" interp="linear"/>'
                         f'<timept time="{_t(start + c["dur"])}" value="{_t(src_start(c) + c["dur"] * sp)}" interp="linear"/></timeMap>')
            warnings['hizli/yavas klip, FCPX\'te kontrol et / speed-changed clip, check in FCPX'] += 1
        elif abs(c['speed'] - 1.0) > 1e-6:
            warnings['ters/negatif hiz normal alindi / reverse speed made normal'] += 1
        if c['vol'] is not None:
            inner.append(f'{indent}  <adjust-volume amount="{c["vol"]:.2f}dB"/>')
        head = f'{indent}<asset-clip {" ".join(attrs)}'
        if not inner and not children:
            return [head + '/>']
        return [head + '>'] + inner + list(children) + [f'{indent}</asset-clip>']

    def lane_of(c):
        return c['track'] if c['kind'] == 'video' else -c['track']

    out = []
    w = out.append
    w('<?xml version="1.0" encoding="UTF-8"?>')
    w('<!DOCTYPE fcpxml>')
    w('<fcpxml version="1.10">')
    w('  <resources>')
    w(f'    <format id="r1" name="FFVideoFormat{H}p{int(round(float(fps)))}" frameDuration="{_t(fd)}" '
      f'width="{W}" height="{H}" colorSpace="1-1-1 (Rec. 709)"/>')
    for v in files.values():
        attrs = [f'id="{v["id"]}"', f'name={quoteattr(v["label"])}', f'start="{_t(v["start"])}"',
                 f'duration="{_t(v["dur"])}"', f'hasVideo="{1 if v["has_v"] else 0}"']
        if v['has_v']:
            attrs += ['format="r1"', 'videoSources="1"']
        if v['has_a']:
            attrs += ['hasAudio="1"', 'audioSources="1"', f'audioChannels="{v["ch"]}"', f'audioRate="{v["sr"]}"']
        url = 'file://' + quote(v['path'], safe='/:')
        w(f'    <asset {" ".join(attrs)}>')
        w(f'      <media-rep kind="original-media" src={quoteattr(url)}/>')
        w('    </asset>')
    w('  </resources>')
    w('  <library>')
    w(f'    <event name={quoteattr(seq_name)}>')
    w(f'      <project name={quoteattr(seq_name)}>')
    w(f'        <sequence format="r1" duration="{_t(total)}" tcStart="0s" tcFormat="NDF" '
      f'audioLayout="stereo" audioRate="48k">')
    w('          <spine>')
    ind = '            '
    for s in spine:
        kids = []
        for c in sorted(s['children'], key=lambda c: c['off']):
            kids += clip_xml(c, ind + '  ', lane=lane_of(c), parent=s)
        if s['gap']:
            head = f'{ind}<gap name="Gap" offset="{_t(s["off"])}" start="0s" duration="{_t(s["dur"])}"'
            if kids:
                w(head + '>')
                out.extend(kids)
                w(f'{ind}</gap>')
            else:
                w(head + '/>')
        else:
            out.extend(clip_xml(s['clip'], ind, children=kids))
    w('          </spine>')
    w('        </sequence>')
    w('      </project>')
    w('    </event>')
    w('  </library>')
    w('</fcpxml>')

    with open(dst, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(out) + '\n')

    return dict(sequence=seq_name, width=W, height=H, fps=float(fps), hours=float(total) / 3600,
                assets=len(files), video_clips=len(vclips), audio_clips=len(aclips),
                warnings=dict(warnings), output=dst)


def format_report(r):
    lines = [f"{r['sequence']}  {r['width']}x{r['height']} @ {r['fps']:g}fps  "
             f"{r['hours'] * 60:.1f} dk/min",
             f"Medya/media: {r['assets']}   Video: {r['video_clips']}   Ses/audio: {r['audio_clips']}"]
    for k, n in r['warnings'].items():
        lines.append(f"  ! {k}: {n}")
    lines.append(f"-> {r['output']}")
    return '\n'.join(lines)


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="KADA XML: Premiere FCP7 XML -> Final Cut Pro X FCPXML")
    ap.add_argument('src', help='Premiere: File > Export > Final Cut Pro XML')
    ap.add_argument('dst', nargs='?', help='output .fcpxml (default: next to input)')
    ap.add_argument('--from', dest='frm', default=None,
                    help='media root to replace (default: detected common folder)')
    ap.add_argument('--to', dest='to', default=None,
                    help='new media root on the Mac, e.g. /Volumes/Disk/Media')
    ap.add_argument('--version', action='version', version=__version__)
    a = ap.parse_args(argv)
    dst = a.dst or os.path.splitext(a.src)[0] + '.fcpxml'
    frm = a.frm if a.frm is not None else (detect_media_root(a.src) if a.to else None)
    try:
        print(format_report(convert(a.src, dst, frm, a.to)))
    except ConvertError as e:
        print(f"HATA / ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
