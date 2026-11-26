#!/usr/bin/env python3
"""KADA XML - Premiere Pro "Final Cut Pro XML" (FCP7 xmeml) -> Final Cut Pro X FCPXML 1.10.

Command line:
  python premiere_to_fcpxml.py INPUT.xml OUTPUT.fcpxml [--from "E:/Media"] [--to "/Volumes/Disk/Media"]

What carries over: cuts, in/out points, track layout, gaps, clip enable state, V1
transitions (as Cross Dissolve), audio transitions (as fades), audio levels, gain and level
keyframes, constant and reversed speed, stills, mixed frame rates, markers.
What does not: effects, color, titles/generators and nests (a marker is left in their place),
video keyframes. Everything not carried is reported.

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

__version__ = "1.0.5"


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


STILL_EXT = ('.png', '.jpg', '.jpeg', '.tif', '.tiff', '.psd', '.bmp', '.gif', '.heic', '.tga', '.exr', '.dpx')


def _fmt_name(w, h, rate):
    """FCPX format name for common sizes; None = custom format (no name)."""
    if rate is None:
        return 'FFVideoFormatRateUndefined'
    size = {(1920, 1080): '1080p', (1280, 720): '720p', (3840, 2160): '2160p'}.get((w, h))
    if size is None:
        return None
    codes = {Fraction(24000, 1001): '2398', Fraction(30000, 1001): '2997', Fraction(60000, 1001): '5994'}
    code = codes.get(Fraction(rate)) or (str(int(rate)) if Fraction(rate).denominator == 1 else None)
    return f'FFVideoFormat{size}{code}' if code else None


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
    seq_tc = seq.find('timecode')
    seq_df = seq_tc is not None and (seq_tc.findtext('displayformat') or '').upper() == 'DF'

    # formats: r1 = sequence, the others describe the media
    formats = {(W, H, fd): 'r1'}

    def fmt_id(w, h, frame_dur):
        key = (w, h, frame_dur)
        if key not in formats:
            formats[key] = f'fmt{len(formats) + 1}'
        return formats[key]

    # ---- assets ----
    files = {}
    nopath = set()  # file ids with no media path: graphics, titles, MOGRTs
    for f in root.iter('file'):
        if f.find('pathurl') is None:
            if f.find('*') is not None:
                nopath.add(f.get('id'))
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
                if local.lower().endswith(('.wav', '.bwf')) and not (
                        os.path.exists(local) or os.path.exists(mapped)):
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
            start = Fraction(round(start * frate)) / frate  # on the media's own frame grid
        out_path = mapped if mapped.startswith('/') else '/' + mapped
        while '//' in out_path:
            out_path = out_path.replace('//', '/')
        parent = os.path.basename(os.path.dirname(local))
        name = f.findtext('name') or os.path.basename(local)
        still = has_v and (local.lower().endswith(STILL_EXT) or f.findtext('duration') is None)
        fmt = None
        if has_v:
            sc = f.find('media/video/samplecharacteristics')
            mw = int(sc.findtext('width') or W) if sc is not None else W
            mh = int(sc.findtext('height') or H) if sc is not None else H
            fmt = fmt_id(mw, mh, None if still else 1 / frate)
        df = tc is not None and (tc.findtext('displayformat') or '').upper() == 'DF' and not still
        if still:
            start, dur = Fraction(0), Fraction(0)
        files[fid] = dict(id=f"r{len(files) + 2}", name=name, parent=parent, path=out_path,
                          start=start, dur=dur, has_v=has_v, has_a=has_a, ch=ch, sr=sr,
                          fmt=fmt, still=still, df=df, frate=frate)

    cnt = Counter(v['name'] for v in files.values())
    for v in files.values():
        v['label'] = f"{v['parent']}_{v['name']}" if cnt[v['name']] > 1 else v['name']

    # ---- clips ----
    def _tr(t):
        if t is None:
            return None
        ticks = t.findtext('cutPointTicks')
        return (int(t.findtext('start')), int(t.findtext('end')),
                (t.findtext('alignment') or 'center').lower(),
                int(ticks) if ticks not in (None, '') else None)

    def _cut(t):
        # FCPX gets no transitions: a two-sided dissolve becomes a cut at its centre,
        # a fade from/to black keeps the clip over the whole fade.
        ts, te, al, ticks = t
        if al in ('center', 'start', 'end') and ticks is not None:
            # Premiere's own cut point inside the transition (254016000000 ticks/s)
            return ts + int(round(Fraction(ticks, 254016000000) * fps))
        if al == 'center':
            return (ts + te) // 2
        if al == 'start':
            return ts
        if al == 'end':
            return te
        return None

    def clip_info(ci, kind, track_no, prev_t=None, next_t=None):
        start = int(ci.findtext('start'))
        end = int(ci.findtext('end'))
        cin_f = int(ci.findtext('in') or 0)
        span = int(ci.findtext('out') or 0) - cin_f
        # Premiere writes -1 for an edge that sits inside a transition. Then in/out run
        # from the start of the transition before to the end of the transition after.
        fade_in = fade_out = 0
        if start < 0:
            if prev_t is not None:
                start = prev_t[0]
                c = _cut(prev_t)
                if c is not None:
                    cin_f += c - start
                    start = c
                fade_in = prev_t[1] - start
            elif end >= 0:
                start = end - span
        if end < 0:
            if next_t is not None:
                end = next_t[1]
                c = _cut(next_t)
                if c is not None:
                    end = c
                fade_out = end - next_t[0]
            elif start >= 0:
                end = start + span
        if start < 0 or end <= start:
            warnings['gecisli klip atlandi / clip inside a transition skipped'] += 1
            return None
        if int(ci.findtext('start')) < 0 or int(ci.findtext('end')) < 0:
            warnings['gecisli klip kenari cozuldu / clip edge inside a transition resolved'] += 1
        name = ci.findtext('name') or ''
        fe = ci.find('file')
        if ci.tag != 'clipitem' or fe is None or fe.get('id') not in files:
            # nest, title, graphic, colour matte...: keep the place, leave a marker
            if ci.find('sequence') is not None:
                what = 'NEST'
                warnings['nest tasinmadi, yerine bosluk + marker / nest not carried, gap + marker left'] += 1
            elif ci.tag == 'generatoritem' or fe is None or fe.get('id') in nopath:
                what = 'YAZI/GRAFIK'
                warnings['yazi/grafik tasinmadi, yerine marker / title/graphic not carried, marker left'] += 1
            else:
                what = 'MEDYA YOK'
                warnings['dosyasiz klip, yerine marker / clip without media, marker left'] += 1
            return dict(placeholder=True, label=f'{what}: {name}', off=Fraction(start) / fps,
                        dur=Fraction(end - start) / fps, kind=kind, track=track_no)
        r = _rate_of(ci, fps)
        cin = Fraction(cin_f) / r
        speed = 1.0
        reverse = False
        vol_db = None
        gain_db = 0.0
        vol_kf = []
        for flt in ci.findall('filter'):
            eid = flt.findtext('effect/effectid') or ''
            ename = flt.findtext('effect/name') or eid
            if eid == 'timeremap':
                for p in flt.iter('parameter'):
                    if p.findtext('parameterid') == 'speed':
                        speed = float(p.findtext('value')) / 100.0
                        if p.findall('keyframe'):
                            warnings['degisken hiz sabit alindi / variable speed made constant'] += 1
                    elif p.findtext('parameterid') == 'reverse':
                        reverse = (p.findtext('value') or '').upper() == 'TRUE'
            elif eid == 'audiolevels':
                for p in flt.iter('parameter'):
                    if p.findtext('parameterid') == 'level':
                        lv = float(p.findtext('value'))
                        kfs = p.findall('keyframe')
                        if kfs:
                            for k in kfs:
                                v = float(k.findtext('value'))
                                vol_kf.append((Fraction(int(float(k.findtext('when')))) / r,
                                               20 * math.log10(v) if v > 0 else -96.0))
                        elif abs(lv - 1.0) > 1e-4:
                            vol_db = 20 * math.log10(lv) if lv > 0 else -96
            elif ename == 'Gain':
                for p in flt.iter('parameter'):
                    if (p.findtext('parameterid') or '').startswith('Gain'):
                        gain_db += float(p.findtext('value') or 0)
            elif eid in ('basic', 'opacity', 'basicmotion', 'audiopan'):
                pass
            else:
                warnings[f'efekt tasinmadi / effect not carried: {ename}'] += 1
        if gain_db:
            vol_db = (vol_db or 0.0) + gain_db
            vol_kf = [(t, v + gain_db) for t, v in vol_kf]
        markers = []
        for m in ci.findall('marker'):
            mi = int(m.findtext('in') or -1)
            if mi < 0:
                continue
            mo = int(m.findtext('out') or -1)
            markers.append((Fraction(mi) / r, Fraction(mo - mi) / r if mo > mi else None,
                            m.findtext('name') or 'Marker', m.findtext('comment') or ''))
        enabled = (ci.findtext('enabled') or 'TRUE').upper() == 'TRUE'
        return dict(fid=fe.get('id'), off=Fraction(start) / fps,
                    dur=Fraction(end - start) / fps, cin=cin, speed=abs(speed) or 1.0,
                    reverse=reverse or speed < 0, vol=vol_db, vol_kf=vol_kf, markers=markers,
                    fade_in=Fraction(max(fade_in, 0)) / fps, fade_out=Fraction(max(fade_out, 0)) / fps,
                    enabled=enabled, kind=kind, track=track_no)

    def track_clips(tr):
        items = [e for e in tr if e.tag in ('clipitem', 'generatoritem', 'transitionitem')]
        for i, e in enumerate(items):
            if e.tag != 'clipitem':
                continue
            pv = items[i - 1] if i > 0 and items[i - 1].tag == 'transitionitem' else None
            nx = items[i + 1] if i + 1 < len(items) and items[i + 1].tag == 'transitionitem' else None
            yield e, _tr(pv), _tr(nx)

    vnode = seq.find('media/video')
    anode = seq.find('media/audio')
    vtracks = vnode.findall('track') if vnode is not None else []
    atracks = anode.findall('track') if anode is not None else []
    vclips, aclips = [], []
    for i, tr in enumerate(vtracks):
        for ci, pt, nt in track_clips(tr):
            c = clip_info(ci, 'video', i, pt, nt)
            if c:
                vclips.append(c)
    # V1 transitions become real FCPX transitions (Cross Dissolve); others are reported
    v1_trans = []
    for i, tr in enumerate(vtracks):
        for t in tr.findall('transitionitem'):
            if i == 0:
                ts, te = int(t.findtext('start')), int(t.findtext('end'))
                if te > ts >= 0:
                    tt = _tr(t)
                    cut = _cut(tt)
                    if cut is None:
                        cut = ts if tt[2] == 'start-black' else te
                    v1_trans.append([Fraction(ts) / fps, Fraction(te) / fps, Fraction(cut) / fps])
                    continue
            warnings['V2+ gecis tasinmadi / transition above V1 not carried'] += 1

    lane_no = 0
    for tr in atracks:
        if int(tr.get('currentExplodedTrackIndex', '0')) > 0:
            continue  # right channel of a split stereo track
        lane_no += 1
        for ci, pt, nt in track_clips(tr):
            c = clip_info(ci, 'audio', lane_no, pt, nt)
            if c:
                aclips.append(c)

    # placeholders (nest, titles...) become markers; an audio twin of a video one is dropped
    holders = [c for c in vclips if c.get('placeholder')]
    seen = {(c['label'], c['off']) for c in holders}
    holders += [c for c in aclips if c.get('placeholder') and (c['label'], c['off']) not in seen]
    vclips = [c for c in vclips if not c.get('placeholder')]
    aclips = [c for c in aclips if not c.get('placeholder')]
    seq_markers = []
    for m in seq.findall('marker'):
        mi = int(m.findtext('in') or -1)
        if mi < 0:
            continue
        mo = int(m.findtext('out') or -1)
        seq_markers.append((Fraction(mi) / fps, Fraction(mo - mi) / fps if mo > mi else None,
                            m.findtext('name') or 'Marker', m.findtext('comment') or ''))
    for h in holders:
        seq_markers.append((h['off'], h['dur'], h['label'], 'Premiere\'den tasinmadi / not carried'))

    total = Fraction(int(seq.findtext('duration') or 0)) / fps
    ends = [c['off'] + c['dur'] for c in vclips + aclips + holders]
    if ends and max(ends) > total:
        total = max(ends)

    # ---- primary storyline = V1 ----
    vclips_lifted = []
    v1 = sorted([c for c in vclips if c['track'] == 0], key=lambda c: c['off'])
    def spd(c):
        return Fraction(c['speed']).limit_denominator(10000)

    def retimed(c):
        return c['reverse'] or (c['speed'] > 0 and abs(c['speed'] - 1.0) > 1e-6)

    def src_start(c):
        # Premiere: on a speed-changed clip in/out are in retimed frames -> convert to source time
        return files[c['fid']]['start'] + (c['cin'] * spd(c) if retimed(c) else c['cin'])

    def rev_range(c):
        # reversed clip: Premiere counts in/out from the END of the media
        fa = files[c['fid']]
        hi = fa['start'] + fa['dur'] - c['cin'] * spd(c)
        return hi, hi - c['dur'] * spd(c)

    def media_grid(c):
        """Frame grid of a clip's own time (FCPX wants starts/offsets inside a clip on it)."""
        fa = files[c['fid']]
        if retimed(c) or not fa['has_v'] or fa['still']:
            return None
        return 1 / fa['frate']

    def snap(x, base, g, floor=False):
        if g is None:
            return x
        k = (x - base) / g
        return base + (math.floor(k + Fraction(1, 1000)) if floor else round(k)) * g

    def local_start(c):
        # FCPX: with a timeMap, the clip's start is in retimed (local) time
        if c['reverse']:
            return Fraction(0)
        if retimed(c):
            return src_start(c) / spd(c)
        # Premiere shows the media frame at or before the in point (floor)
        return snap(src_start(c), files[c['fid']]['start'], media_grid(c), floor=True)

    def in_parent(s, t):
        """timeline time t -> local time of spine item s. FCPX wants the parent's start on
        its media grid and (child offset - parent start) on the sequence grid."""
        return s['lstart'] + snap(t - s['off'], Fraction(0), fd)

    spine = []
    cur = Fraction(0)
    top_lane = max([c['track'] for c in vclips] + [0]) + 1
    for c in v1:
        if c['off'] < cur:
            # never drop it: lift it onto a lane above the storyline
            warnings['V1 ust uste binen klip ust kanala alindi / overlapping V1 clip moved up'] += 1
            c['track'] = top_lane
            vclips_lifted.append(c)
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
        s['markers'] = []

    def parent_for(off):
        for s in spine:
            if s['off'] <= off < s['off'] + s['dur']:
                return s
        return spine[-1]

    for c in [c for c in vclips if c['track'] > 0] + vclips_lifted + aclips:
        parent_for(c['off'])['children'].append(c)
    for m in seq_markers:
        parent_for(m[0])['markers'].append(m)

    def marker_xml(indent, start, dur, name, note):
        a = f'start="{_t(start)}" duration="{_t(dur if dur else fd)}" value={quoteattr(name)}'
        if note:
            a += f' note={quoteattr(note)}'
        return f'{indent}<marker {a}/>'

    def item_markers(s, indent):
        return [marker_xml(indent, in_parent(s, m[0]), m[1], m[2], m[3])
                for m in sorted(s['markers'], key=lambda m: m[0])]

    def clip_xml(c, indent, lane=None, parent=None, children=(), extra_markers=()):
        fa = files[c['fid']]
        src_en = None
        if c['kind'] == 'video' and fa['has_a']:
            src_en = 'video'
        if c['kind'] == 'audio' and fa['has_v']:
            src_en = 'audio'
        off = c['off'] if parent is None else in_parent(parent, c['off'])
        start = local_start(c)
        attrs = [f'ref="{fa["id"]}"', f'name={quoteattr(fa["label"])}', f'offset="{_t(off)}"',
                 f'start="{_t(start)}"', f'duration="{_t(c["dur"])}"']
        if lane is not None:
            attrs.append(f'lane="{lane}"')
        if src_en:
            attrs.append(f'srcEnable="{src_en}"')
        if fa['has_v'] and not fa['still']:
            attrs += [f'format="{fa["fmt"]}"', f'tcFormat="{"DF" if fa["df"] else "NDF"}"']
        if not c['enabled']:
            attrs.append('enabled="0"')
        inner = []
        if c['reverse']:
            hi, lo = rev_range(c)
            inner.append(f'{indent}  <timeMap><timept time="0s" value="{_t(hi)}" interp="linear"/>'
                         f'<timept time="{_t(c["dur"])}" value="{_t(lo)}" interp="linear"/></timeMap>')
            warnings['ters klip, FCPX\'te kontrol et / reversed clip, check in FCPX'] += 1
        elif retimed(c):
            sp = spd(c)
            # linear map 0 -> 0 that covers the clip's whole local range
            inner.append(f'{indent}  <timeMap><timept time="0s" value="0s" interp="linear"/>'
                         f'<timept time="{_t(start + c["dur"])}" value="{_t(src_start(c) + c["dur"] * sp)}" interp="linear"/></timeMap>')
            warnings['hizli/yavas klip, FCPX\'te kontrol et / speed-changed clip, check in FCPX'] += 1
        if c['kind'] == 'audio':
            parts = ''
            if c['fade_in'] > 0:
                parts += f'<fadeIn type="easeIn" duration="{_t(c["fade_in"])}"/>'
            if c['fade_out'] > 0:
                parts += f'<fadeOut type="easeOut" duration="{_t(c["fade_out"])}"/>'
            if parts:
                warnings['ses gecisi fade olarak tasindi / audio transition carried as fade'] += 1
            if c['vol_kf'] and not retimed(c):
                parts += '<keyframeAnimation>' + ''.join(
                    f'<keyframe time="{_t(fa["start"] + t)}" value="{v:.2f}dB"/>'
                    for t, v in c['vol_kf']) + '</keyframeAnimation>'
            elif c['vol_kf']:
                warnings['ses keyframe tasinmadi / audio keyframes not carried'] += 1
            amt = f' amount="{c["vol"]:.2f}dB"' if c['vol'] is not None and not (c['vol_kf'] and not retimed(c)) else ''
            if parts:
                inner.append(f'{indent}  <adjust-volume{amt}><param name="amount">{parts}</param></adjust-volume>')
            elif amt:
                inner.append(f'{indent}  <adjust-volume{amt}/>')
        marks = list(extra_markers)
        if not retimed(c):
            lo_t, hi_t = start, start + c['dur']
            for t, d, nm, note in c['markers']:
                mt = snap(fa['start'] + t, fa['start'], media_grid(c))
                if lo_t <= mt < hi_t:
                    marks.append(marker_xml(indent + '  ', mt, d, nm, note))
        # Final Cut crashes on a still image inside <asset-clip>; it takes it as <video>
        tag = 'video' if fa['still'] else 'asset-clip'
        if fa['still']:
            attrs = [a for a in attrs if not a.startswith('srcEnable=')]
        head = f'{indent}<{tag} {" ".join(attrs)}'
        if not inner and not children and not marks:
            return [head + '/>']
        return [head + '>'] + inner + list(children) + marks + [f'{indent}</{tag}>']

    def lane_of(c):
        return c['track'] if c['kind'] == 'video' else -c['track']

    # ---- V1 transitions: one per edit, inside the media FCPX actually has ----
    def handles(c):
        """seconds of media before the clip's start and after its end (timeline time)"""
        fa = files[c['fid']]
        if fa['still'] or c['reverse']:
            return None, None
        sp = spd(c) if retimed(c) else Fraction(1)
        s0 = src_start(c)
        return (s0 - fa['start']) / sp, (fa['start'] + fa['dur'] - (s0 + c['dur'] * sp)) / sp

    def floor_fd(x):
        return math.floor(x / fd) * fd

    by_cut = {}
    for a, b, cut in v1_trans:
        if cut in by_cut:            # fade to black + fade from black on one edit -> one dissolve
            by_cut[cut][0] = min(by_cut[cut][0], a)
            by_cut[cut][1] = max(by_cut[cut][1], b)
        else:
            by_cut[cut] = [a, b]
    fitted = []
    for cut, (a, b) in sorted(by_cut.items()):
        left = next((x for x in spine if x['off'] + x['dur'] == cut), None)
        right = next((x for x in spine if x['off'] == cut), None)
        if left is not None and not left['gap']:
            after = handles(left['clip'])[1]
            if after is not None:
                b = min(b, cut + floor_fd(max(after, Fraction(0))))
            b = min(b, left['off'] + left['dur'] + (right['dur'] if right else 0))
        if right is not None and not right['gap']:
            before = handles(right['clip'])[0]
            if before is not None:
                a = max(a, cut - floor_fd(max(before, Fraction(0))))
        if left is None and right is None:
            continue
        a, b = max(a, Fraction(0)), min(b, total)
        if b - a < fd:
            warnings['gecis icin medya yok, kesim yapildi / no media for transition, made a cut'] += 1
            continue
        fitted.append((a, b - a, cut))
    if fitted:
        warnings['gecis Cross Dissolve olarak tasindi / transitions carried as Cross Dissolve'] += len(fitted)
    if len(by_cut) < len(v1_trans):
        warnings['ayni kesimdeki gecisler birlestirildi / transitions on one edit merged'] += \
            len(v1_trans) - len(by_cut)

    out = []
    w = out.append
    w('<?xml version="1.0" encoding="UTF-8"?>')
    w('<!DOCTYPE fcpxml>')
    w('<fcpxml version="1.10">')
    w('  <resources>')
    for (fw, fh, ffd), fid_ in formats.items():
        nm = _fmt_name(fw, fh, None if ffd is None else 1 / ffd)
        a = [f'id="{fid_}"'] + ([f'name="{nm}"'] if nm else [])
        if ffd is not None:
            a.append(f'frameDuration="{_t(ffd)}"')
        a += [f'width="{fw}"', f'height="{fh}"', 'colorSpace="1-1-1 (Rec. 709)"']
        w(f'    <format {" ".join(a)}/>')
    for v in files.values():
        attrs = [f'id="{v["id"]}"', f'name={quoteattr(v["label"])}', f'start="{_t(v["start"])}"',
                 f'duration="{_t(v["dur"])}"', f'hasVideo="{1 if v["has_v"] else 0}"']
        if v['has_v']:
            attrs += [f'format="{v["fmt"]}"', 'videoSources="1"']
        if v['has_a']:
            attrs += ['hasAudio="1"', 'audioSources="1"', f'audioChannels="{v["ch"]}"', f'audioRate="{v["sr"]}"']
        url = 'file://' + quote(v['path'], safe='/:')
        w(f'    <asset {" ".join(attrs)}>')
        w(f'      <media-rep kind="original-media" src={quoteattr(url)}/>')
        w('    </asset>')
    if fitted:
        w('    <effect id="fxCD" name="Cross Dissolve" uid="FxPlug:4731E73A-8DAC-4113-9A30-AE85B1761265"/>')
    w('  </resources>')
    w('  <library>')
    w(f'    <event name={quoteattr(seq_name)}>')
    w(f'      <project name={quoteattr(seq_name)}>')
    w(f'        <sequence format="r1" duration="{_t(total)}" tcStart="0s" tcFormat="{"DF" if seq_df else "NDF"}" '
      f'audioLayout="stereo" audioRate="48k">')
    w('          <spine>')
    ind = '            '
    pending = list(fitted)

    def flush(before):
        # a transition sits in the spine on its edit, between the two items it joins
        while pending and (before is None or pending[0][2] <= before):
            o, d, _cut_at = pending.pop(0)
            w(f'{ind}<transition name="Cross Dissolve" offset="{_t(o)}" duration="{_t(d)}">')
            w(f'{ind}  <filter-video ref="fxCD" name="Cross Dissolve"/>')
            w(f'{ind}</transition>')

    for s in spine:
        flush(s['off'])
        kids = []
        for c in sorted(s['children'], key=lambda c: c['off']):
            kids += clip_xml(c, ind + '  ', lane=lane_of(c), parent=s)
        marks = item_markers(s, ind + '  ')
        if s['gap']:
            head = f'{ind}<gap name="Gap" offset="{_t(s["off"])}" start="0s" duration="{_t(s["dur"])}"'
            if kids or marks:
                w(head + '>')
                out.extend(kids + marks)
                w(f'{ind}</gap>')
            else:
                w(head + '/>')
        else:
            out.extend(clip_xml(s['clip'], ind, children=kids, extra_markers=marks))
    flush(None)
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
