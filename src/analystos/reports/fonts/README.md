# Bundled report fonts

- `DejaVuSans*.ttf`, `DejaVuSansMono.ttf`: DejaVu Sans (Bitstream Vera / public-domain licence, [LICENSE_DEJAVU](LICENSE_DEJAVU)),
  the primary face: Latin, Latin-extended, Greek, Cyrillic and report symbols, with real bold and oblique styles.
- `NotoSansCJKsc-VF.ttf`: Noto Sans CJK SC from the
  [Noto CJK upstream repository](https://github.com/notofonts/noto-cjk/blob/main/Sans/Variable/TTF/NotoSansCJKsc-VF.ttf)
  under the [SIL Open Font License 1.1](OFL.txt), the fallback for Han, Hiragana, Katakana and Hangul.
  SHA-256 `990c807e79c25662a5a9ecf7f971baeb2bf2eab9a559e5ecf15cdfdb8561d21f`.

`fpdf2` embeds only the glyphs each report uses. A character neither font covers (Indic scripts, emoji) is
mapped, folded or printed as `?` by `pdf_text`, never dropped silently.
