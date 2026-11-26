# KADA XML

**Premiere → Final Cut Pro X.** Premiere Pro sekanslarını Final Cut Pro X'e (FCPXML) taşıyan küçük, ücretsiz bir program.
Windows ve Mac'te çalışır, kurulum gerektirmez.

*KADA XML is a small free tool that moves Premiere Pro sequences into Final Cut Pro X (FCPXML). English below.*

## İndir

[Releases](../../releases) sayfasından:
- **Windows:** `KADA-XML-Windows.zip` → içindeki `.exe`
- **Mac:** `KADA-XML-macOS.zip` → `KADA-XML.app`
  (imzasız olduğu için ilk açılışta: sağ tık → **Aç**)

## Kullanım

1. Premiere'de sekansı seç → **File → Export → Final Cut Pro XML…**
2. Programda bu XML'i seç.
3. Medya Mac'te başka bir yerdeyse, **"Mac'teki yeri"** alanına o klasörü yaz
   (ör. `/Volumes/Disk/Dugun_Isim`). Program XML'deki ortak medya klasörünü kendisi bulur.
4. **Dönüştür**'e bas → `.fcpxml` dosyası çıkar.
5. Final Cut Pro X: **File → Import → XML…** Klipler kırmızı gelirse: seç → **File → Relink Files**.

## Ne taşınır, ne taşınmaz

| Taşınır | Taşınmaz |
|---|---|
| Kesimler, in/out noktaları | Efektler, Lumetri / renk |
| Track düzeni (V1 ana storyline, V2+ üstte, sesler altta) | Geçişler |
| Boşluklar (gap) | Yazılar, MOGRT, generator |
| Klip açık/kapalı durumu | Keyframe'ler |
| Sabit ses seviyesi (dB) | Nested sekans içeriği |
| Sabit hız (kontrol et) | |
| WAV/BWF timecode (senkron) | |

Taşınmayan her şey dönüştürmeden sonra raporda yazar.

## Komut satırı

Python 3.8+ ile, ek paket gerekmez:

```
python premiere_to_fcpxml.py SEKANS.xml SEKANS.fcpxml --to "/Volumes/Disk/Is"
python app.py          # pencereli arayüz
python tests/test_convert.py [gercek.xml]   # öz test
```

---

## English

1. In Premiere select the sequence → **File → Export → Final Cut Pro XML…**
2. Open the XML in the app. If the media lives elsewhere on the Mac, put that folder in
   **"On the Mac"**. The app detects the common media folder in the XML and swaps it.
3. **Convert** → import the `.fcpxml` in FCPX via **File → Import → XML…**
   (relink if clips show as missing).

Carries cuts, in/out points, track layout (V1 = primary storyline, other video tracks as
lanes above, audio as lanes below), gaps, enable state, constant audio level, constant
speed and BWF timecode for WAV files. Effects, color, transitions, titles and keyframes are
not carried and are listed in the report. Standard library only. MIT license.

Builds: push a tag `v*` and GitHub Actions builds the Windows `.exe` and macOS `.app`
and attaches them to the release.
