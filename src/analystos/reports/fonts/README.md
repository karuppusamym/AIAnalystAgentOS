# Bundled report font

`NotoSansCJKsc-VF.ttf` is Noto Sans CJK SC, downloaded from the
[Noto CJK upstream repository](https://github.com/notofonts/noto-cjk/blob/main/Sans/Variable/TTF/NotoSansCJKsc-VF.ttf).
It covers Han, Hiragana, Katakana, Hangul, Latin, Greek, Cyrillic, and common report symbols.
The complete [SIL Open Font License 1.1](OFL.txt) is bundled beside the font.
The font's SHA-256 is
`990c807e79c25662a5a9ecf7f971baeb2bf2eab9a559e5ecf15cdfdb8561d21f`.

`fpdf2` embeds only glyphs used in each report. Unsupported characters are replaced
with `?` by `pdf_text` so rendering remains reliable without silent missing glyphs.
