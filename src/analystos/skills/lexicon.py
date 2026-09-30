"""Multilingual column-name vocabulary: the same column read the same way whatever language names it.

The crawler's rules (skills/catalog) and the role playbook read English name tokens (`amount`, `date`,
`id`, ...). A German, Spanish, French, Portuguese or Tamil table carries the same data under other words,
and a rule that only knows English would classify it differently and so test different hypotheses (the
held-out transfer suite measured exactly that). This module maps a name to canonical English tokens:

* transliteration-safe folding: NFKC, case-folded, German umlauts spelled out (``ü`` -> ``ue``, ``ß`` -> ``ss``,
  the spelling ASCII-only schemas already use), other Latin diacritics dropped (``número`` -> ``numero``);
  non-Latin scripts (Tamil) are kept whole, never stripped of their vowel signs;
* a synonym table (word -> English tokens) that only holds words with no English meaning of their own, so an
  English name canonicalises to itself;
* German compounds split on a known tail (``belegdatum`` -> ``beleg date``, ``kundenklasse`` -> ``customer class``).

Names only ever add a hint; the verdict-bearing decisions (identifier, measure, segment order) rest on
profiled types and value distributions (skills/profiling, agents/investigator).
"""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

_UMLAUT = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss", "æ": "ae",
                         "ø": "oe", "œ": "oe"})
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")

# word -> English token(s). Grouped by language for review; a word listed twice must mean the same thing.
_DE = {
    "datum": "date", "zeit": "time", "zeitpunkt": "timestamp", "zeitstempel": "timestamp",
    "tage": "days", "stunde": "hours", "stunden": "hours", "minuten": "minutes", "sekunden": "seconds", "dauer": "duration",
    "betrag": "amount", "summe": "total", "preis": "price", "kosten": "cost", "umsatz": "revenue", "gebuehr": "fee",
    "steuer": "tax", "rabatt": "discount", "gehalt": "salary", "saldo": "balance", "zahlung": "payment",
    "anzahl": "count", "menge": "quantity", "stueck": "units", "nr": "no", "nummer": "no", "kennung": "id",
    "schluessel": "key", "kunde": "customer", "kunden": "customer", "klasse": "class", "kategorie": "category",
    "typ": "type", "stufe": "tier", "gruppe": "group", "abteilung": "department", "waehrung": "currency",
    "stadt": "city", "bezeichnung": "name", "beschreibung": "description", "kommentar": "comment",
    "bemerkung": "remarks", "notiz": "note", "anteil": "share", "prozent": "percent", "ist": "is",
    "rechnung": "invoice", "bestellung": "order", "auftrag": "order", "forderung": "receivable",
    "verbindlichkeit": "payable", "zahlungsziel": "payment terms", "mitarbeiter": "employee",
    "sachbearbeiter": "clerk", "lieferant": "supplier", "artikel": "item", "produkt": "product", "filiale": "store",
    "sendung": "shipment", "lieferung": "delivery", "erstellt": "created",
    "geaendert": "updated", "geschlossen": "closed", "geoeffnet": "opened", "ueberfaellig": "overdue",
    "abgeschrieben": "written off", "kanal": "channel", "quelle": "source", "vertrieb": "sales",
    "telefon": "phone", "adresse": "address", "strasse": "street", "plz": "postcode", "geburtsdatum": "birth date",
    "bestell": "order", "versand": "shipping", "gesamt": "total",
}
_ES_PT = {
    "fecha": "date", "hora": "time", "dia": "day", "dias": "days", "horas": "hours", "minutos": "minutes",
    "segundos": "seconds", "duracion": "duration", "duracao": "duration", "importe": "amount", "monto": "amount",
    "valor": "value", "precio": "price", "preco": "price", "costo": "cost", "custo": "cost", "ingresos": "revenue",
    "impuesto": "tax", "descuento": "discount", "desconto": "discount", "salario": "salary", "pago": "payment",
    "pagamento": "payment", "cantidad": "quantity", "quantidade": "quantity", "numero": "no", "nro": "no",
    "identificador": "id", "clave": "key", "chave": "key", "cliente": "customer", "clase": "class", "classe": "class",
    "categoria": "category", "tipo": "type", "estado": "status", "situacao": "status", "nivel": "level",
    "grupo": "group", "departamento": "department", "moneda": "currency", "moeda": "currency", "pais": "country",
    "ciudad": "city", "cidade": "city", "nombre": "name", "nome": "name", "descripcion": "description",
    "descricao": "description", "comentario": "comment", "observaciones": "remarks", "porcentaje": "percent",
    "tasa": "rate", "taxa": "rate", "factura": "invoice", "fatura": "invoice", "pedido": "order", "ventas": "sales",
    "vendas": "sales", "empleado": "employee", "funcionario": "employee", "proveedor": "supplier",
    "fornecedor": "supplier", "producto": "product", "produto": "product", "tienda": "store", "loja": "store",
    "almacen": "warehouse", "envio": "shipment", "entrega": "delivery", "creado": "created", "criado": "created",
    "actualizado": "updated", "cerrado": "closed", "canal": "channel", "fuente": "source", "origen": "source",
    "equipo": "team", "segmento": "segment", "correo": "email", "telefono": "phone", "direccion": "address",
    "endereco": "address", "vencido": "overdue", "ganado": "won", "ganho": "won", "industria": "industry",
}
_FR = {
    "montant": "amount", "prix": "price", "cout": "cost", "quantite": "quantity", "statut": "status",
    "categorie": "category", "jours": "days", "heures": "hours", "libelle": "label", "devise": "currency",
    "commande": "order", "fournisseur": "supplier", "magasin": "store", "livraison": "delivery",
    "adresse": "address", "ville": "city",
}
_TA = {  # Tamil
    "தேதி": "date", "நேரம்": "time", "தொகை": "amount", "விலை": "price", "செலவு": "cost", "எண்": "no",
    "எண்ணிக்கை": "count", "அளவு": "quantity", "பெயர்": "name", "வகை": "type", "நிலை": "status",
    "நாட்கள்": "days", "மணிநேரம்": "hours", "வாடிக்கையாளர்": "customer", "பகுதி": "region", "குறியீடு": "code",
    "விளக்கம்": "description", "கருத்து": "comment", "நாணயம்": "currency", "பிரிவு": "department",
    "விற்பனை": "sales", "பொருள்": "product", "கடை": "store", "கட்டணம்": "fee", "தள்ளுபடி": "discount",
    "ஊழியர்": "employee", "விலைப்பட்டியல்": "invoice", "ஆர்டர்": "order", "அனுப்புதல்": "shipment",
}
SYNONYMS: dict[str, tuple[str, ...]] = {k: tuple(v.split()) for lang in (_DE, _ES_PT, _FR, _TA) for k, v in lang.items()}
# German compounds end in one of these (``belegdatum``, ``kundenklasse``, ``rechnungsbetrag``); the head is
# canonicalised on its own, with a linking ``s``/``n`` dropped when the bare head is a known word.
COMPOUND_TAILS = ("datum", "zeitpunkt", "nummer", "betrag", "summe", "preis", "kosten", "anzahl", "menge", "klasse",
                  "kategorie", "gruppe", "stufe", "dauer", "stunden", "tage", "anteil", "kennung", "typ",
                  "bezeichnung", "beschreibung", "waehrung", "zahlung", "kanal", "quelle", "status", "region")
# Plural endings (Tamil -கள், Spanish/Portuguese -es/-s, German -en/-n) dropped only when the stem is a known word.
PLURALS = ("கள்", "en", "es", "n", "s")
# A key named head-first (`numero_factura`, `id_cliente`, `nr_kunde`) reads like `invoice_no`: the id word moves last.
_ID_HEADS = frozenset({"numero", "nro", "nr", "nummer", "id", "identificador", "kennung", "எண்"})


def fold(text: str) -> str:
    """Umlauts spelled out and Latin diacritics dropped (case kept for camelCase splitting); other scripts unchanged."""
    s = unicodedata.normalize("NFKC", text or "").translate(_UMLAUT)
    out: list[str] = []
    for ch in unicodedata.normalize("NFKD", s):
        if unicodedata.combining(ch) and out and out[-1].isascii():
            continue  # a Latin accent; a Tamil vowel sign follows a non-ASCII base and is kept
        out.append(ch)
    return unicodedata.normalize("NFC", "".join(out))


def raw_tokens(name: str) -> list[str]:
    """Folded name -> lower-case tokens: ASCII parts split on snake/camel case, other scripts kept whole."""
    parts, cur = [], []
    for ch in fold(name):
        if ch.isalnum() or (unicodedata.category(ch).startswith("M") and cur):  # a vowel sign belongs to its word
            cur.append(ch)
        elif cur:
            parts.append("".join(cur))
            cur = []
    parts.append("".join(cur))
    out: list[str] = []
    for part in filter(None, parts):
        if part.isascii():
            out.extend(t.lower() for t in _CAMEL.findall(part))
        else:
            out.append(part.lower())
    return out


@lru_cache(maxsize=4096)
def _canonical_word(word: str) -> tuple[str, ...]:
    if word in SYNONYMS:
        return SYNONYMS[word]
    for suffix in PLURALS:
        if word.endswith(suffix) and word[: -len(suffix)] in SYNONYMS:
            return SYNONYMS[word[: -len(suffix)]]
    for tail in sorted(COMPOUND_TAILS, key=len, reverse=True):
        if word.endswith(tail) and len(word) >= len(tail) + 3:
            head = word[: -len(tail)]
            if head not in SYNONYMS and head[-1:] in ("s", "n") and head[:-1] in SYNONYMS:
                head = head[:-1]
            return (*SYNONYMS.get(head, (head,)), *SYNONYMS.get(tail, (tail,)))
    return (word,)


def canonical_tokens(name: str) -> list[str]:
    """English tokens for a column or table name in any supported language; English names map to themselves."""
    words = raw_tokens(name)
    if len(words) > 1 and words[0] in _ID_HEADS and words[1] not in _ID_HEADS:
        words = words[1:] + words[:1]
    return [t for w in words for t in _canonical_word(w)]
