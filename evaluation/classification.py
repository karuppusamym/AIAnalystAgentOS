"""N-12: measured error rates of the catalog's rule classification across domains and languages.

A labelled corpus of tables (five domains) whose columns are named in English, German, Spanish and Tamil. Each
column carries its true role family and each table its true domain; the rules (skills/catalog, read through the
multilingual lexicon) classify every table, and the error rates are reported per language and per domain, with
calibration: how often a classification the catalog calls confident (>= ENRICH_CONFIDENCE, the level at which no
enrichment is asked for) is wrong. Deterministic, no model, no services.

Two splits. `tuned`: the corpus the lexicon was extended against, so its rates are an upper bound for these words.
`unseen`: two further domains written before their results were looked at and never used to change the lexicon,
the fairer estimate for new names, and where the residual error stays visible (German `lagerbestand`, Tamil
`மறு_ஆர்டர்_நிலை`). Neither is a claim about a live source.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from analystos.connectors.base import DiscoveredAsset, DiscoveredColumn

LANGUAGES = ("en", "de", "es", "ta")
FAMILY = {"identifier": "key", "foreign_key": "key", "date": "time", "timestamp": "time", "measure": "measure",
          "amount": "measure", "duration": "measure", "percent": "measure", "flag": "flag", "dimension": "attribute",
          "code": "attribute", "name": "attribute", "text": "attribute", "geo": "attribute", "contact": "attribute"}

# (domain, table names en/de/es/ta, [(column names en/de/es/ta, data type, true family)])
Table = tuple[str, tuple[str, ...], list[tuple[tuple[str, ...], str, str]]]
CORPUS: list[Table] = [
    ("finance", ("invoices", "rechnungen", "facturas", "விலைப்பட்டியல்கள்"), [
        (("invoice_id", "rechnung_nr", "numero_factura", "விலைப்பட்டியல்_எண்"), "integer", "key"),
        (("invoice_date", "rechnungsdatum", "fecha_factura", "விலைப்பட்டியல்_தேதி"), "date", "time"),
        (("customer_id", "kunden_nr", "id_cliente", "வாடிக்கையாளர்_எண்"), "integer", "key"),
        (("total_amount", "gesamtbetrag", "importe_total", "மொத்த_தொகை"), "numeric", "measure"),
        (("tax_amount", "steuerbetrag", "importe_impuesto", "வரி_தொகை"), "numeric", "measure"),
        (("currency_code", "waehrung", "moneda", "நாணயம்"), "text", "attribute"),
        (("is_paid", "ist_bezahlt", "pagado", "செலுத்தப்பட்டது"), "boolean", "flag"),
        (("payment_status", "zahlungsstatus", "estado_pago", "கட்டண_நிலை"), "text", "attribute"),
    ]),
    ("sales", ("orders", "bestellungen", "pedidos", "ஆர்டர்கள்"), [
        (("order_id", "bestell_nr", "numero_pedido", "ஆர்டர்_எண்"), "integer", "key"),
        (("order_date", "bestelldatum", "fecha_pedido", "ஆர்டர்_தேதி"), "date", "time"),
        (("quantity", "menge", "cantidad", "அளவு"), "integer", "measure"),
        (("unit_price", "stueckpreis", "precio_unitario", "அலகு_விலை"), "numeric", "measure"),
        (("discount_pct", "rabatt_prozent", "porcentaje_descuento", "தள்ளுபடி_சதவீதம்"), "double", "measure"),
        (("sales_channel", "vertriebskanal", "canal_venta", "விற்பனை_வழி"), "text", "attribute"),
        (("store_region", "filialregion", "region_tienda", "கடை_பகுதி"), "text", "attribute"),
    ]),
    ("hr", ("employees", "mitarbeiter", "empleados", "ஊழியர்கள்"), [
        (("employee_id", "personal_nr", "numero_empleado", "ஊழியர்_எண்"), "integer", "key"),
        (("hire_date", "eintrittsdatum", "fecha_contratacion", "சேர்ந்த_தேதி"), "date", "time"),
        (("department", "abteilung", "departamento", "பிரிவு"), "text", "attribute"),
        (("salary", "gehalt", "salario", "சம்பளம்"), "numeric", "measure"),
        (("tenure_days", "zugehoerigkeit_tage", "dias_antiguedad", "பணிக்கால_நாட்கள்"), "integer", "measure"),
        (("is_active", "ist_aktiv", "activo", "செயலில்"), "boolean", "flag"),
        (("job_title", "stellenbezeichnung", "puesto", "பதவி"), "text", "attribute"),
    ]),
    ("logistics", ("shipments", "sendungen", "envios", "அனுப்புதல்கள்"), [
        (("shipment_id", "sendungs_nr", "numero_envio", "அனுப்புதல்_எண்"), "integer", "key"),
        (("shipped_at", "versandzeitpunkt", "fecha_envio", "அனுப்பிய_நேரம்"), "timestamp", "time"),
        (("carrier", "spediteur", "transportista", "கொண்டுசெல்பவர்"), "text", "attribute"),
        (("weight_kg", "gewicht_kg", "peso_kg", "எடை_கிலோ"), "double", "measure"),
        (("transit_hours", "transitdauer_stunden", "horas_transito", "பயண_மணிநேரம்"), "double", "measure"),
        (("delivered_late", "verspaetet", "entrega_tardia", "தாமதம்"), "boolean", "flag"),
        (("destination_country", "zielland", "pais_destino", "சேருமிட_நாடு"), "text", "attribute"),
    ]),
    ("customer", ("customers", "kunden", "clientes", "வாடிக்கையாளர்கள்"), [
        (("customer_id", "kunden_nr", "id_cliente", "வாடிக்கையாளர்_எண்"), "integer", "key"),
        (("signup_date", "anmeldedatum", "fecha_alta", "பதிவு_தேதி"), "date", "time"),
        (("segment", "segment", "segmento", "பிரிவு"), "text", "attribute"),
        (("lifetime_value", "kundenwert", "valor_vida", "வாழ்நாள்_மதிப்பு"), "numeric", "measure"),
        (("churned", "abgewandert", "abandono", "விலகியது"), "boolean", "flag"),
        (("email", "email", "correo", "மின்னஞ்சல்"), "text", "attribute"),
        (("country", "land", "pais", "நாடு"), "text", "attribute"),
    ]),
]
UNSEEN: list[Table] = [
    ("supply_chain", ("inventory_levels", "lagerbestand", "inventario", "சரக்கு_இருப்பு"), [
        (("warehouse_id", "lager_id", "id_almacen", "கிடங்கு_எண்"), "integer", "key"),
        (("snapshot_date", "stichtag", "fecha_corte", "பதிவு_நாள்"), "date", "time"),
        (("on_hand_qty", "bestand_menge", "cantidad_disponible", "கையிருப்பு_அளவு"), "integer", "measure"),
        (("reorder_point", "meldebestand", "punto_reorden", "மறு_ஆர்டர்_நிலை"), "integer", "measure"),
        (("supplier_name", "lieferantenname", "nombre_proveedor", "சப்ளையர்_பெயர்"), "text", "attribute"),
        (("is_backordered", "nachbestellt", "pendiente", "நிலுவையில்"), "boolean", "flag"),
        (("unit_cost", "stueckkosten", "costo_unitario", "அலகு_செலவு"), "numeric", "measure"),
    ]),
    ("marketing", ("campaigns", "kampagnen", "campanas", "பிரச்சாரங்கள்"), [
        (("campaign_id", "kampagnen_nr", "id_campana", "பிரச்சார_எண்"), "integer", "key"),
        (("start_date", "startdatum", "fecha_inicio", "தொடக்க_தேதி"), "date", "time"),
        (("channel", "kanal", "canal", "ஊடகம்"), "text", "attribute"),
        (("budget", "budget", "presupuesto", "வரவு_செலவு"), "numeric", "measure"),
        (("clicks", "klicks", "clics", "சொடுக்குகள்"), "integer", "measure"),
        (("conversion_rate", "konversionsrate", "tasa_conversion", "மாற்று_விகிதம்"), "double", "measure"),
        (("is_paused", "pausiert", "pausada", "இடைநிறுத்தம்"), "boolean", "flag"),
    ]),
]
SPLITS = {"tuned": CORPUS, "unseen": UNSEEN}


@dataclass
class Rates:
    columns: int = 0
    column_errors: int = 0
    confident: int = 0  # classifications at or above ENRICH_CONFIDENCE
    confident_wrong: int = 0
    tables: int = 0
    domain_errors: int = 0
    domain_wrong: int = 0  # a named domain that is not the truth; the rest of the errors abstained as `generic`
    wrong: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        def r(a: int, b: int) -> float | None:
            return round(a / b, 4) if b else None
        return {"columns": self.columns, "column_error_rate": r(self.column_errors, self.columns),
                "confident": self.confident, "confident_error_rate": r(self.confident_wrong, self.confident),
                "confident_wrong_share": r(self.confident_wrong, self.columns), "tables": self.tables,
                "domain_error_rate": r(self.domain_errors, self.tables),
                "domain_wrong_rate": r(self.domain_wrong, self.tables), "wrong": self.wrong}


def run(split: str = "tuned") -> dict[str, Any]:
    """Rates per language, per domain and overall, plus a confidence calibration table (0.1-wide bins)."""
    from analystos.skills.catalog import ENRICH_CONFIDENCE, infer_table_semantics

    corpus = SPLITS[split]
    by_lang = {lang: Rates() for lang in LANGUAGES}
    by_domain = {d: Rates() for d, _, _ in corpus}
    bins: dict[float, list[int]] = {}
    for domain, tables, cols in corpus:
        for i, lang in enumerate(LANGUAGES):
            asset = DiscoveredAsset(source_name=tables[i], name=tables[i], schema_name="eval",
                                    columns=[DiscoveredColumn(name=names[i], data_type=dtype) for names, dtype, _ in cols])
            sem = infer_table_semantics(asset)
            for rates in (by_lang[lang], by_domain[domain]):
                rates.tables += 1
                if sem.domain != domain:
                    rates.domain_errors += 1
                    rates.domain_wrong += int(sem.domain != "generic")
                    rates.wrong.append(f"{lang} table {tables[i]}: domain {sem.domain} (truth {domain})")
            for (names, _, truth), cs in zip(cols, sem.columns, strict=True):
                ok = FAMILY.get(cs.semantic_role) == truth
                confident = cs.confidence >= ENRICH_CONFIDENCE
                b = min(0.9, int(cs.confidence * 10) / 10)
                bins.setdefault(b, [0, 0])
                bins[b][0] += 1
                bins[b][1] += int(ok)
                for rates in (by_lang[lang], by_domain[domain]):
                    rates.columns += 1
                    rates.confident += int(confident)
                    if not ok:
                        rates.column_errors += 1
                        rates.confident_wrong += int(confident)
                        rates.wrong.append(f"{lang} {names[i]}: {cs.semantic_role} @ {cs.confidence} (truth {truth})")
    total = Rates()
    for r in by_lang.values():
        for k in ("columns", "column_errors", "confident", "confident_wrong", "tables", "domain_errors", "domain_wrong"):
            setattr(total, k, getattr(total, k) + getattr(r, k))
    return {"split": split, "languages": {k: v.as_dict() for k, v in by_lang.items()},
            "domains": {k: v.as_dict() for k, v in by_domain.items()},
            "overall": {k: v for k, v in total.as_dict().items() if k != "wrong"},
            "calibration": [{"confidence": f"{b:.1f}-{b + 0.1:.1f}", "columns": n, "accuracy": round(ok / n, 4)}
                            for b, (n, ok) in sorted(bins.items())]}
