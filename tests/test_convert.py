"""Self-check: build a small synthetic Premiere XML, convert it, and verify that
every clip lands at the same timeline position / source in-point / lane.
Run: python tests/test_convert.py [optional_real_premiere.xml]"""
import os
import sys
import tempfile
import xml.etree.ElementTree as ET
from fractions import Fraction as F

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import premiere_to_fcpxml as core  # noqa: E402

FPS = 25


def _file(fid, name, path, dur, tc_frame, video=True, ch=2):
    v = ('<video><samplecharacteristics><width>1920</width><height>1080</height>'
         '</samplecharacteristics></video>') if video else ''
    return (f'<file id="{fid}"><name>{name}</name><pathurl>file://localhost/{path}</pathurl>'
            f'<rate><timebase>{FPS}</timebase><ntsc>FALSE</ntsc></rate><duration>{dur}</duration>'
            f'<timecode><rate><timebase>{FPS}</timebase><ntsc>FALSE</ntsc></rate><frame>{tc_frame}</frame></timecode>'
            f'<media>{v}<audio><samplecharacteristics><samplerate>48000</samplerate></samplecharacteristics>'
            f'<channelcount>{ch}</channelcount></audio></media></file>')


def _clip(cid, name, fid, start, end, cin, files_seen, filexml, level=None, speed=None):
    f = f'<file id="{fid}"/>' if fid in files_seen else filexml
    files_seen.add(fid)
    lv = ''
    if level is not None:
        lv = ('<filter><effect><name>Audio Levels</name><effectid>audiolevels</effectid>'
              f'<parameter><parameterid>level</parameterid><value>{level}</value></parameter></effect></filter>')
    if speed is not None:
        lv += ('<filter><effect><name>Time Remap</name><effectid>timeremap</effectid>'
               f'<parameter><parameterid>speed</parameterid><value>{speed}</value></parameter></effect></filter>')
    return (f'<clipitem id="{cid}"><name>{name}</name><enabled>TRUE</enabled>'
            f'<rate><timebase>{FPS}</timebase><ntsc>FALSE</ntsc></rate>'
            f'<start>{start}</start><end>{end}</end><in>{cin}</in><out>{cin + end - start}</out>{f}{lv}</clipitem>')


def make_sample(path):
    seen = set()
    fa = _file('f1', 'A001.MP4', 'D%3a/Media/Job/CAM%20A/A001.MP4', 5000, 90000)
    fb = _file('f2', 'B001.MP4', 'D%3a/Media/Job/CAM%20B/B001.MP4', 5000, 90100)
    fw = _file('f3', 'Tr1.WAV', 'D%3a/Media/Job/Audio/T01/Tr1.WAV', 6000, 89000, video=False, ch=1)
    fw2 = _file('f4', 'Tr1.WAV', 'D%3a/Media/Job/Audio/T02/Tr1.WAV', 6000, 95000, video=False, ch=1)
    v1 = (_clip('c1', 'A001.MP4', 'f1', 0, 250, 0, seen, fa) +
          _clip('c2', 'A001.MP4', 'f1', 300, 700, 400, seen, fa, speed=50))  # gap 250-300, 50% slow
    v2 = _clip('c3', 'B001.MP4', 'f2', 120, 480, 10, seen, fb)
    a1l = _clip('c4', 'A001.MP4', 'f1', 300, 700, 400, seen, fa, level=0.5)
    a1r = _clip('c5', 'A001.MP4', 'f1', 300, 700, 400, seen, fa)
    a2 = (_clip('c6', 'Tr1.WAV', 'f3', 0, 260, 31, seen, fw) +
          _clip('c7', 'Tr1.WAV', 'f4', 280, 700, 5, seen, fw2))
    xml = (f'<?xml version="1.0" encoding="UTF-8"?><xmeml version="4"><sequence id="s1"><name>TEST</name>'
           f'<duration>700</duration><rate><timebase>{FPS}</timebase><ntsc>FALSE</ntsc></rate><media>'
           '<video><format><samplecharacteristics><width>1920</width><height>1080</height></samplecharacteristics></format>'
           f'<track>{v1}</track><track>{v2}</track></video><audio>'
           f'<track currentExplodedTrackIndex="0">{a1l}</track><track currentExplodedTrackIndex="1">{a1r}</track>'
           f'<track currentExplodedTrackIndex="0">{a2}</track>'
           '</audio></media></sequence></xmeml>')
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(xml)


def _s(x):
    x = x[:-1]
    if '/' in x:
        a, b = x.split('/')
        return F(int(a), int(b))
    return F(x)


def _speed(c):
    for f in c.findall('filter'):
        if f.findtext('effect/effectid') == 'timeremap':
            for p in f.iter('parameter'):
                if p.findtext('parameterid') == 'speed':
                    return F(p.findtext('value')) / 100
    return F(1)


def expected(src):
    seq = ET.parse(src).getroot().find('.//sequence')
    fps = core._rate_of(seq)
    exp = []
    for i, t in enumerate(seq.find('media/video').findall('track')):
        for c in t.findall('clipitem'):
            # Premiere: in/out of a speed-changed clip are retimed frames
            exp.append(('v', i, F(int(c.findtext('start'))) / fps, F(int(c.findtext('end'))) / fps,
                        F(int(c.findtext('in'))) / fps * _speed(c)))
    ln = 0
    for t in seq.find('media/audio').findall('track'):
        if t.get('currentExplodedTrackIndex', '0') != '0':
            continue
        for c in t.findall('clipitem'):
            exp.append(('a', ln, F(int(c.findtext('start'))) / fps, F(int(c.findtext('end'))) / fps,
                        F(int(c.findtext('in'))) / fps))
        ln += 1
    return sorted(exp)


def _src(el):
    """Source time at the clip's local start (through the timeMap, if any)."""
    ls = _s(el.get('start'))
    tm = el.find('timeMap')
    if tm is None:
        return ls
    pts = tm.findall('timept')
    assert _s(pts[0].get('time')) == 0 and _s(pts[0].get('value')) == 0, 'timeMap must start at 0'
    t1, v1 = _s(pts[-1].get('time')), _s(pts[-1].get('value'))
    assert ls + _s(el.get('duration')) <= t1, 'clip outside timeMap'
    return ls * v1 / t1


def got(dst):
    r = ET.parse(dst).getroot()
    starts = {a.get('id'): _s(a.get('start')) for a in r.iter('asset')}
    out = []
    for el in r.find('.//spine'):
        off, ls = _s(el.get('offset')), _s(el.get('start'))
        if el.tag == 'asset-clip':
            out.append(('v', 0, off, off + _s(el.get('duration')), _src(el) - starts[el.get('ref')]))
        for c in el:
            if c.tag != 'asset-clip':
                continue
            o = off + _s(c.get('offset')) - ls
            lane = int(c.get('lane'))
            out.append(('v' if lane > 0 else 'a', lane if lane > 0 else -lane - 1,
                        o, o + _s(c.get('duration')), _src(c) - starts[c.get('ref')]))
    return sorted(out)


def check(src, to=None):
    d = tempfile.mkdtemp()
    dst = os.path.join(d, 'out.fcpxml')
    r = core.convert(src, dst, core.detect_media_root(src) if to else None, to)
    e, g = expected(src), got(dst)
    assert e == g, f"mismatch:\n{e[:5]}\n{g[:5]}"
    return r, dst


if __name__ == '__main__':
    d = tempfile.mkdtemp()
    sample = os.path.join(d, 'sample.xml')
    make_sample(sample)
    r, dst = check(sample, to='/Volumes/Disk/Media')
    txt = open(dst, encoding='utf-8').read()
    # detected media root = D:/Media/Job  ->  /Volumes/Disk/Media
    assert 'file:///Volumes/Disk/Media/CAM%20A/A001.MP4' in txt, 'path mapping'
    assert 'T01_Tr1.WAV' in txt and 'T02_Tr1.WAV' in txt, 'duplicate names'
    assert 'adjust-volume amount="-6.02dB"' in txt, 'volume'
    assert r['audio_clips'] == 3 and r['video_clips'] == 3, r
    print('sample OK:', core.format_report(r).splitlines()[1])
    for extra in sys.argv[1:]:
        r, _ = check(extra)
        print('OK:', extra, r['video_clips'] + r['audio_clips'], 'clips')
    print('ALL OK')
