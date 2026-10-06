"""Recepción de lecturas del lector RFID (webhook del FX) y token por lector."""

import json
import logging

from django.http import JsonResponse
from django.utils import timezone

from wms.models import LectorRFID, RfidScan

logger = logging.getLogger(__name__)


def _es_hexadecimal_epc(value):
    s = (value or "")
    if isinstance(s, bytes):
        try:
            s = s.decode("utf-8", errors="replace")
        except Exception:
            s = ""
    s = str(s).strip().replace(" ", "").replace(":", "").replace("-", "")
    if not s:
        return False
    if len(s) < 8 or len(s) > 64:
        return False
    try:
        int(s, 16)
        return True
    except ValueError:
        return False


def _extract_epc_raw(item):
    """Devuelve el EPC hex raw de un item de scan dict o string.

    Soporta:
      - str: hex directo
      - dict top-level keys comunes FX/ZDS
      - anidado {tag:{epcHex:..}, meta:{..}} (Zebra Data Services SDK)
      - anidado {data:{hex:.., source:..}} (FX EventReport)
      - fuzzy find por substring (hex / epc / tag) con profundidad hasta 6.
    """
    if item is None:
        return None
    if isinstance(item, bytes):
        try:
            item = item.decode("utf-8", errors="replace")
        except Exception:
            item = None
    if isinstance(item, str):
        s = item.strip()
        return s if s else None

    if not isinstance(item, dict):
        return None

    top_level_keys = (
        "idHex", "data", "epc", "EPC", "tagID", "tidHex", "epcHex", "hex",
        "epcId", "epcID", "tagEpc", "tag_epc", "tid", "value", "code",
        "raw", "rawValue", "tag_id",
    )
    candidates = []
    for key in top_level_keys:
        v = item.get(key)
        if v is None:
            continue
        if isinstance(v, dict):
            for sub in top_level_keys:
                sv = v.get(sub)
                if isinstance(sv, (str, bytes)) and sv:
                    candidates.append(sv)
        elif isinstance(v, (str, bytes)):
            candidates.append(v)

    tag = item.get("tag")
    if isinstance(tag, dict):
        for sub in top_level_keys:
            sv = tag.get(sub)
            if isinstance(sv, (str, bytes)) and sv:
                candidates.append(sv)
        fuzzy_tag = _find_by_key_substr(tag, ["hex", "epc", "tag"])
        if isinstance(fuzzy_tag, (str, bytes)) and fuzzy_tag:
            candidates.append(fuzzy_tag)

    data = item.get("data")
    if isinstance(data, dict):
        for sub in top_level_keys:
            sv = data.get(sub)
            if isinstance(sv, (str, bytes)) and sv:
                candidates.append(sv)
        fuzzy_data = _find_by_key_substr(data, ["hex", "epc", "tag"])
        if isinstance(fuzzy_data, (str, bytes)) and fuzzy_data:
            candidates.append(fuzzy_data)

    reads = item.get("reads")
    if isinstance(reads, list):
        for r in reads:
            if isinstance(r, (str, bytes)) and r:
                candidates.append(r)
                break

    fuzzy_top = _find_by_key_substr(item, ["hex", "epc", "tag"])
    if isinstance(fuzzy_top, (str, bytes)) and fuzzy_top:
        candidates.append(fuzzy_top)

    for c in candidates:
        if isinstance(c, bytes):
            try:
                c = c.decode("utf-8", errors="replace")
            except Exception:
                continue
        if isinstance(c, str):
            s = c.strip()
            if s:
                return s
    return None


def _extract_int(value):
    try:
        if value is None:
            return None
        if isinstance(value, bool):
            return 1 if value else 0
        if isinstance(value, float):
            return int(value) if value.is_integer() else None
        return int(value)
    except (TypeError, ValueError):
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return None
            try:
                if "." in stripped or "e" in stripped.lower():
                    as_float = float(stripped)
                    return int(as_float) if as_float.is_integer() else None
                return int(stripped)
            except (TypeError, ValueError):
                return None
        return None


def _extract_float(value):
    try:
        if value is None:
            return None
        if isinstance(value, bool):
            return 1.0 if value else 0.0
        return float(value)
    except (TypeError, ValueError):
        if isinstance(value, str) and value.strip():
            try:
                return float(value.strip())
            except (TypeError, ValueError):
                return None
        return None


def _find_by_key_substr(d, substrings):
    """Búsqueda recursiva de keys por substring a profundidad 6.

    Retorna el primer valor (no list/tuple/dict) cuya key haga match;
    si no, retorna None.
    """
    if not isinstance(d, dict) and not isinstance(d, (list, tuple)):
        return None

    def _recurse(obj, depth):
        if depth > 6:
            return None
        if isinstance(obj, dict):
            for key, value in obj.items():
                k = str(key).lower().replace("_", "").replace("-", "")
                matched = False
                for s in substrings:
                    s_norm = s.lower().replace("_", "").replace("-", "")
                    if s_norm in k:
                        matched = True
                        break
                if matched:
                    if value is not None and not isinstance(value, (list, tuple, dict)):
                        return value
                found = _recurse(value, depth + 1)
                if found is not None:
                    return found
            return None
        if isinstance(obj, (list, tuple)):
            for x in obj:
                found = _recurse(x, depth + 1)
                if found is not None:
                    return found
            return None
        return None

    return _recurse(d, 0)


_ANTENNA_INT_KEYS = (
    "antenna", "antennaID", "antennaId", "antennaPort", "antennaPortName",
    "port", "ant", "source", "antenna_number", "antennaNumber",
    "port_no", "portNo", "antPort", "ant_port", "readerPort",
    "reader_port", "inputPort", "channel", "channelIndex",
)
_RSSI_FLOAT_KEYS = (
    "peakRssi", "rssi", "rssiDbm", "peakRssiDbm", "rssi_value",
    "rssiValue", "peak_rssi", "peakRssiValue", "signal_strength",
    "signalStrength", "rssi_db", "rssiDb", "signalDb", "signalLevel",
    "signal_level", "rssiPeak", "rxRssi", "rx_rssi", "tagRSSI",
)


def _antenna_from_value(raw):
    """Convierte enteros, floats enteros, y strings estilo 'ANT-1' / 'Port#3'."""
    as_int = _extract_int(raw)
    if as_int is not None:
        return as_int
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return None
        import re as _re
        m = _re.search(r"\d+", stripped)
        if m:
            try:
                return int(m.group(0))
            except Exception:
                return None
    return None


def _extract_antenna_rssi(item, fallback_antenna=None, fallback_rssi=None):
    antenna = None
    rssi = None
    if isinstance(item, dict):
        for key in _ANTENNA_INT_KEYS:
            if key in item:
                v = _antenna_from_value(item.get(key))
                if v is not None:
                    antenna = v
                    break
        if antenna is None:
            meta = item.get("meta")
            if isinstance(meta, dict):
                for key in _ANTENNA_INT_KEYS:
                    if key in meta:
                        v = _antenna_from_value(meta.get(key))
                        if v is not None:
                            antenna = v
                            break
        if antenna is None:
            data = item.get("data")
            if isinstance(data, dict):
                for key in _ANTENNA_INT_KEYS:
                    if key in data:
                        v = _antenna_from_value(data.get(key))
                        if v is not None:
                            antenna = v
                            break
        if antenna is None:
            fuzzy_raw = _find_by_key_substr(item, ["ant", "port", "channel", "source"])
            antenna = _antenna_from_value(fuzzy_raw)

        for key in _RSSI_FLOAT_KEYS:
            if key in item:
                v = _extract_float(item.get(key))
                if v is not None:
                    rssi = v
                    break
        if rssi is None:
            meta = item.get("meta")
            if isinstance(meta, dict):
                for key in _RSSI_FLOAT_KEYS:
                    if key in meta:
                        v = _extract_float(meta.get(key))
                        if v is not None:
                            rssi = v
                            break
        if rssi is None:
            data = item.get("data")
            if isinstance(data, dict):
                for key in _RSSI_FLOAT_KEYS:
                    if key in data:
                        v = _extract_float(data.get(key))
                        if v is not None:
                            rssi = v
                            break
        if rssi is None:
            fuzzy_rssi = _find_by_key_substr(item, ["rssi", "signal", "dbm", "db"])
            rssi = _extract_float(fuzzy_rssi)

    if antenna is None:
        antenna = _antenna_from_value(fallback_antenna)
    if rssi is None:
        rssi = _extract_float(fallback_rssi)
    return antenna, rssi


def lector_desde_request(request):
    """Lector activo dueño del token (header X-RFID-Token, Bearer o ?token=)."""
    token = (request.headers.get("X-RFID-Token") or request.GET.get("token") or "").strip()
    if not token:
        auth = request.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    if not token:
        return None
    return LectorRFID.objects.filter(token=token, activo=True).first()


def recibir_lecturas(request, lector):
    """Parsea el payload del FX (JSON, form o texto) y guarda un ``RfidScan`` por EPC.

    ``request`` es el ``HttpRequest`` de Django (lee ``body`` y ``POST`` crudos).
    """
    # Declarado FUERA del try para que los logger.info/warning de abajo
    # (fuera del bloque try) no causen NameError si algo falló en medio.
    body = ""
    debug_payload = {
        "content_type": request.content_type or "",
        "method": request.method,
        "remote_addr": request.META.get("REMOTE_ADDR"),
    }
    try:
        remote_addr = request.META.get("REMOTE_ADDR")
        raw_body = request.body.decode("utf-8", errors="replace")
        body = raw_body
        debug_payload["body_len"] = len(raw_body)
        debug_payload["body_prefix"] = raw_body[:512]
        logger.info(
            "RFID receive from %s ct=%s body[:4096]=%s",
            remote_addr,
            request.content_type,
            raw_body[:4096],
        )

        # --- PARSEO MULTI CONTENT-TYPE (porque FX puede mandar text/plain, form, urlencoded, JSON)
        data = None
        parse_attempts = []

        # 1) JSON directo (mejor caso)
        try:
            if raw_body and raw_body.strip():
                data = json.loads(raw_body)
                parse_attempts.append("json_loads:OK")
        except (json.JSONDecodeError, ValueError, Exception) as e:
            parse_attempts.append(f"json_loads:FAIL:{type(e).__name__}")
            data = None

        # 2) request.POST (x-www-form-urlencoded) — FXs viejos a veces usan esto
        if data is None and request.POST:
            try:
                qd = request.POST.dict()
                # Si contiene una key llamada "data"/"payload" con JSON adentro: intentar parsearla
                for wrapper in ["data", "payload", "body", "json", "tags_json"]:
                    if wrapper in qd and isinstance(qd[wrapper], str):
                        try:
                            parsed_inner = json.loads(qd[wrapper])
                            qd[wrapper] = parsed_inner
                            break
                        except Exception:
                            pass
                data = qd
                parse_attempts.append("request.POST.dict:OK")
            except Exception as e:
                parse_attempts.append(f"request.POST.dict:FAIL:{type(e).__name__}")

        # 3) text/plain pero con EPCs separados por newlines (lista de strings sin corchetes)
        if data is None and raw_body and raw_body.strip():
            stripped = raw_body.strip()
            if "\n" in stripped or "," in stripped:
                # Lista separada por comas o saltos de línea (posiblemente con espacios)
                parts = [p.strip() for p in stripped.replace(",", "\n").splitlines() if p.strip()]
                # Si todo parece hex (chars [0-9a-fA-F: - ])
                def _looks_hex(s):
                    if not s or len(s) < 8:
                        return False
                    cl = s.replace(" ", "").replace(":", "").replace("-", "")
                    return all(c in "0123456789abcdefABCDEF" for c in cl)
                if parts and all(_looks_hex(p) for p in parts):
                    data = [{"epc": p} for p in parts]
                    parse_attempts.append("split_newlines_epc:OK")

        # 4) Si sigue None: cuerpo dict vacío {} o [] con intento final limpiar espacios
        if data is None and raw_body and raw_body.strip():
            try:
                cleaned = raw_body.strip()
                # caso texto plano con objeto JSON (sin header correcto)
                data = json.loads(cleaned)
                parse_attempts.append("json_loads_cleaned:OK")
            except Exception as e:
                parse_attempts.append(f"json_loads_cleaned:FAIL:{type(e).__name__}")
                data = None

        debug_payload["parse_attempts"] = parse_attempts

        # --- Extracción items + fallback (igual que antes)
        items = []
        fallback_antenna = None
        fallback_rssi = None
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = (
                data.get("tagData")
                or data.get("tags")
                or data.get("events")
                or data.get("eventList")
                or data.get("data")
                or data.get("items")
                or data.get("reads")
                or data.get("readEvents")
                or data.get("tagReadEvents")
                or [data]
            )
            fallback_antenna = (
                data.get("antenna")
                or data.get("antennaID")
                or data.get("antennaPort")
                or data.get("port")
                or data.get("ant")
                or data.get("source")
                or data.get("antenna_number")
                or data.get("antennaNumber")
                or data.get("port_no")
                or data.get("portNo")
                or _find_by_key_substr(data, ["ant", "port"])
            )
            fallback_rssi = (
                data.get("peakRssi")
                or data.get("rssi")
                or data.get("rssiDbm")
                or data.get("peakRssiDbm")
                or data.get("rssi_value")
                or data.get("rssiValue")
                or data.get("peak_rssi")
                or data.get("peakRssiValue")
                or data.get("signal_strength")
                or data.get("signalStrength")
                or _find_by_key_substr(data, ["rssi", "signal"])
            )

            debug_payload["body_dict_keys"] = sorted(data.keys())
        else:
            debug_payload["body_dict_keys"] = None
            debug_payload["fallback_antenna_raw_type"] = type(data).__name__

        if not isinstance(items, list):
            items = []
        debug_payload["fallback_antenna_final"] = (
            str(fallback_antenna)[:200] if fallback_antenna is not None else None
        )
        debug_payload["fallback_rssi_final"] = (
            str(fallback_rssi)[:200] if fallback_rssi is not None else None
        )
        debug_payload["items_raw_len"] = len(items)

        if not isinstance(items, list):
            debug_payload["error"] = "Invalid data format"
            return JsonResponse(
                {"status": "error", "message": "Invalid data format", "debug": debug_payload},
                status=400,
            )

        tags_to_create = []
        debug_items = []
        seen_epcs_in_this_batch = set()
        for idx, item in enumerate(items):
            if isinstance(item, dict) and item.get("isHeartBeat") is True:
                continue
            epc_raw = _extract_epc_raw(item)
            if not epc_raw:
                # Debug: tag item sin EPC, igual guardamos info para saber por qué
                debug_items.append(
                    {
                        "i": idx,
                        "item_type": type(item).__name__,
                        "keys_list": sorted(item.keys())[:20] if isinstance(item, dict) else None,
                        "epc_raw": None,
                        "antenna_final": None,
                        "rssi_final": None,
                        "item_str": (str(item)[:200] if not isinstance(item, (dict, list)) else None),
                    }
                )
                continue
            # limpia / normaliza hex (minusculas, sin separadores)
            epc_norm = (
                epc_raw.strip()
                .replace(" ", "")
                .replace(":", "")
                .replace("-", "")
                .lower()
            )
            if not epc_norm or len(epc_norm) < 8:
                debug_items.append(
                    {
                        "i": idx,
                        "epc_raw": epc_raw[:40] if isinstance(epc_raw, str) else epc_raw,
                        "skip_reason": f"len<8 epc={epc_norm}",
                    }
                )
                continue
            # Si ya estaba dentro del mismo request (duplicado fx reader), omitimos 2da insert
            if epc_norm in seen_epcs_in_this_batch:
                debug_items.append(
                    {"i": idx, "epc_norm": epc_norm, "skip_reason": "dup_in_batch"}
                )
                continue
            seen_epcs_in_this_batch.add(epc_norm)
            antenna, rssi = _extract_antenna_rssi(
                item, fallback_antenna=fallback_antenna, fallback_rssi=fallback_rssi
            )
            # --- DEEP debug del item (se sube a INFO para que aparezca en Vercel)
            debug_item_entry = {
                "i": idx,
                "item_type": type(item).__name__,
                "epc_raw_len": (len(epc_raw) if isinstance(epc_raw, (str, bytes)) else None),
                "epc_raw_head": epc_raw[:32] if isinstance(epc_raw, str) else None,
                "epc_norm": epc_norm,
                "antenna_final": antenna,
                "rssi_final": rssi,
                "fallback_antenna_used": (
                    str(fallback_antenna)[:80] if fallback_antenna is not None else None
                ),
                "fallback_rssi_used": (
                    str(fallback_rssi)[:80] if fallback_rssi is not None else None
                ),
            }
            if isinstance(item, dict):
                debug_item_entry["keys_top20"] = sorted(item.keys())[:20]
                try:
                    dump = json.dumps(item, ensure_ascii=False, default=str)
                except Exception:
                    dump = "<unserializable>"
                debug_item_entry["item_json_prefix"] = dump[:300]
                debug_item_entry["item_json_len"] = len(dump)
            else:
                debug_item_entry["item_str_prefix"] = str(item)[:200]

            # Dump a Vercel logs
            excerpt = dict(debug_item_entry)
            try:
                excerpt_json = json.dumps(excerpt, ensure_ascii=False, default=str)
            except Exception:
                excerpt_json = f"<excerpt_unserializable keys={list(excerpt.keys())}>"
            logger.info(
                "RFID receive tag[%s] epc=%s len=%s antenna=%s rssi=%s excerpt=%s",
                idx,
                epc_norm,
                len(epc_norm),
                antenna,
                rssi,
                excerpt_json[:1400],
            )
            debug_items.append(debug_item_entry)

            tags_to_create.append(
                RfidScan(
                    empresa_id=lector.empresa_id,
                    lector=lector,
                    epc=epc_norm,
                    reader_ip=remote_addr,
                    antenna=antenna,
                    rssi=rssi,
                )
            )

        debug_payload["items"] = debug_items

        if tags_to_create:
            # Log resumen DEL REQUEST ENTERO (nivel INFO).
            summary = {
                "count": len(tags_to_create),
                "items_raw_len": len(items),
                "unique_epcs_sample": sorted([s.epc for s in tags_to_create[:5]]),
                "antenna_values": sorted({s.antenna for s in tags_to_create if s.antenna is not None}),
                "rssi_values_sample": sorted([float(s.rssi) for s in tags_to_create if s.rssi is not None])[:10],
            }
            logger.info(
                "RFID receive request: created=%s summary=%s body_sample=%s",
                len(tags_to_create),
                json.dumps(summary, ensure_ascii=False, default=str),
                (body[:200] if isinstance(body, str) else "(binary)"),
            )
            RfidScan.objects.bulk_create(tags_to_create, batch_size=200)
            LectorRFID.objects.filter(pk=lector.pk).update(ultima_lectura=timezone.now())
            debug_payload["created_epcs_sample"] = summary["unique_epcs_sample"]
            debug_payload["antenna_values"] = summary["antenna_values"]
            debug_payload["rssi_values_sample"] = summary["rssi_values_sample"]
        else:
            logger.warning(
                "RFID receive request: 0 tags creadas. Tipo items=%s len_items=%s body_sample=%s",
                type(items).__name__,
                len(items) if hasattr(items, "__len__") else "(n/a)",
                (body[:500] if isinstance(body, str) else "(binary)"),
            )
            debug_payload["warn"] = (
                "0 tags creadas. Tipo items=%s len=%s"
                % (type(items).__name__, len(items) if hasattr(items, "__len__") else "n/a")
            )

        return JsonResponse(
            {"status": "success", "count": len(tags_to_create), "debug": debug_payload}
        )
    except json.JSONDecodeError as e:
        debug_payload["error"] = f"JSON invalido: {str(e)}"
        return JsonResponse(
            {"status": "error", "message": f"JSON invalido: {str(e)}", "debug": debug_payload},
            status=400,
        )
    except Exception as e:
        logger.exception("RFID receive error")
        debug_payload["error"] = str(e)
        debug_payload["error_type"] = type(e).__name__
        return JsonResponse(
            {"status": "error", "message": str(e), "debug": debug_payload},
            status=500,
        )
