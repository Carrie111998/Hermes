# Fonts

Three families, all licensed under the SIL Open Font License 1.1, which permits
embedding and redistribution.

| File | Family | Upstream |
|---|---|---|
| `bricolage.woff2` | Bricolage Grotesque | https://fonts.google.com/specimen/Bricolage+Grotesque |
| `newsreader.woff2` | Newsreader | https://fonts.google.com/specimen/Newsreader |
| `dmmono400.woff2`, `dmmono500.woff2` | DM Mono | https://fonts.google.com/specimen/DM+Mono |

Each file is the upstream Latin subset, reduced further to the punctuation this
page uses and — for the two variable faces — instanced to a single optical size
while keeping the weight axis intact. That takes the set from roughly 340 KB to
78 KB, which is what makes inlining them as data URIs reasonable.

To regenerate after a copy change that introduces new glyphs:

```
pip install fonttools brotli

UNI='U+0020-007E,U+00A0,U+00A9,U+00AD,U+00B0,U+00B7,U+2010-2015,U+2018-201A,U+201C-201E,U+2020-2022,U+2026,U+2030,U+2039-203A,U+2044,U+2212,U+2192,U+2713,U+2014'

# Variable faces: pin the axes the page does not vary, keep weight.
fonttools varLib.instancer BricolageGrotesque.ttf wdth=100 opsz=48 -o bric.ttf
pyftsubset bric.ttf --unicodes="$UNI" --layout-features='kern,liga,calt' \
  --flavor=woff2 --output-file=bricolage.woff2

fonttools varLib.instancer Newsreader.ttf opsz=20 -o news.ttf
pyftsubset news.ttf --unicodes="$UNI" --layout-features='kern,liga,calt,onum' \
  --flavor=woff2 --output-file=newsreader.woff2

# DM Mono ships static instances.
pyftsubset DMMono-Regular.ttf --unicodes="$UNI" --layout-features='kern,liga' \
  --flavor=woff2 --output-file=dmmono400.woff2
pyftsubset DMMono-Medium.ttf --unicodes="$UNI" --layout-features='kern,liga' \
  --flavor=woff2 --output-file=dmmono500.woff2
```

Then `python3 ../build.py` to re-inline them.
