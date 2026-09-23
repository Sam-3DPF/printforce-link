"""Reconcile the fleet with the printer config 3DPF couriers down (U4).

On a throttled interval the reconciler pulls `GET /api/bridge/printers/config`, and for
every printer the cloud is delivering a NOT-yet-delivered access code for, it:

  1. writes the code (+ address) to the durable local store (its permanent home), then
  2. adds the printer to the RUNNING fleet if it isn't there and an address is known
     (so a printer added in the web wizard appears without a bridge restart, U2), then
  3. ACKs the delivery so the cloud deletes its copy of the code (courier hand-off done).

A printer WITH an access code is stored, added, and ACKed. Once the code is
delivered, later pulls still carry `local_ip` with no code. An unchanged
stale pin must not overwrite an address SSDP or a live session just learned.
A changed pin for a printer already in the fleet is only a candidate: the
fleet adopts it after the stored address fails and the pin proves the serial.
A stored printer that is not running has nothing connected to prove against,
so that pin is applied directly. The store, not this pull, is what
re-connects stored printers after a restart (app.py builds the fleet from it
at startup).
"""
import logging
import time
from typing import Optional

from .config import PrinterConfig

logger = logging.getLogger(__name__)

_DEFAULT_RECONCILE_INTERVAL_SECONDS = 60.0


def _model_code(entry) -> str:
    """A DevModel code 3DPF may carry. 3DPF's config pull does not send one yet."""
    for key in ("model", "model_name"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _stored_model(store, bambu_id: str) -> str:
    for cfg in store.configs():
        if cfg.bambu_id == bambu_id:
            return cfg.model
    return ""


class ConfigReconciler:
    def __init__(self, dpf, fleet, store,
                 interval_seconds: float = _DEFAULT_RECONCILE_INTERVAL_SECONDS,
                 monotonic=time.monotonic):
        self._dpf = dpf
        self._fleet = fleet
        self._store = store
        self._interval = interval_seconds
        self._monotonic = monotonic
        self._last = None                       # None -> pull on the first tick
        # Last couriered pin per serial. An unchanged stale 86.x pin must not
        # yank a locally learned 8.x address back every 60s.
        self._cloud_pins = {}

    def tick(self) -> None:
        """One reconcile pass, throttled to `interval_seconds`. Never raises — a courier
        or network failure is swallowed and retried next tick, so the report loop is
        unaffected."""
        now = self._monotonic()
        if self._last is not None and now - self._last < self._interval:
            return
        self._last = now
        try:
            self._reconcile()
        except Exception as e:
            logger.warning("config reconcile failed (%s); will retry next tick", type(e).__name__)

    def _reconcile(self) -> None:
        config = self._dpf.get_printers_config() or {}

        # Tombstones (U5) FIRST, before the printers loop below: 3DPF deleted these
        # serials and is telling the bridge to stop connecting to them. Drop from the
        # live fleet and the durable store now, so that if the SAME serial also shows up
        # in "printers" this tick (e.g. a re-adopt raced the tombstone's ack and the
        # backend delivered a stale one — see adopt_bridge_printer clearing the
        # tombstone), the printer that 3DPF is actively couriering wins: it gets added
        # back by the loop below instead of being removed after the fact. A serial no
        # longer in the fleet (already torn down, or never connected) is a no-op —
        # remove_printer and store.remove are both idempotent by design.
        removed = []
        for bambu_id in (config.get("remove") or []):
            if not bambu_id:
                continue
            self._fleet.remove_printer(bambu_id)
            self._store.remove(bambu_id)
            removed.append(bambu_id)

        printers = config.get("printers") or []
        acks = []
        for p in printers:
            bambu_id = p.get("bambu_id")
            access_code = p.get("access_code")   # present only while the code is undelivered
            if not bambu_id:
                continue
            local_ip = p.get("local_ip")
            model = _model_code(p)
            if model:
                self._store.set_model(bambu_id, model)
            if not access_code:
                # Already delivered: 3DPF still sends the pinned local_ip. Apply a
                # reserved-IP edit without waiting for a new access code or a restart.
                self._refresh_stored_ip(bambu_id, local_ip)
                continue
            # 1. Durably store the code first — the store is its permanent home, so we
            #    must have written it before ACKing the cloud to delete its copy.
            self._store.upsert(bambu_id, access_code, local_ip)
            if model:
                self._store.set_model(bambu_id, model)
            # 2. Push the code into the running fleet so the printer connects without a
            #    restart (U2). The cloud only sends a code while it is UNdelivered, so a code
            #    arriving here for a printer ALREADY in the fleet means the operator
            #    re-adopted with a corrected access code (the #1 onboarding mistake — the
            #    first code was mistyped, so the printer joined the fleet OFFLINE). Rebuild
            #    that member — remove first, since add_printer is a no-op when the serial is
            #    already present — so it reconnects with the new credential instead of
            #    stranding on the old code until a manual restart. Needs an address either way.
            if local_ip:
                if self._fleet.by_id(bambu_id) is not None:
                    self._fleet.remove_printer(bambu_id)
                self._fleet.add_printer(PrinterConfig(
                    bambu_id=bambu_id, ip=local_ip, access_code=access_code,
                    model=model or _stored_model(self._store, bambu_id),
                ))
            # 3. Queue the ACK so the cloud deletes the code.
            printer_id, config_version = p.get("printer_id"), p.get("config_version")
            if printer_id and config_version:
                acks.append({"printer_id": printer_id, "config_version": config_version})
        if acks or removed:
            self._dpf.ack_printers_config(acks, removed=removed)

    def _refresh_stored_ip(self, bambu_id: str, local_ip: Optional[str]) -> None:
        """Hand a changed 3DPF pin to the fleet, or apply it when nothing is connected.

        ``_cloud_pins`` remembers the last pin so an unchanged stale address
        cannot yank a learned one back. The store is not written here for a
        running printer: ``on_address`` does that after the proof.
        """
        if not local_ip or not self._store.has(bambu_id):
            return
        current = None
        for cfg in self._store.configs():
            if cfg.bambu_id == bambu_id:
                current = cfg
                break
        last_pin = self._cloud_pins.get(bambu_id)
        self._cloud_pins[bambu_id] = local_ip
        if last_pin == local_ip:
            return
        if current is None or current.ip == local_ip:
            return
        if self._fleet.by_id(bambu_id) is not None:
            propose = getattr(self._fleet, "propose_address", None)
            if callable(propose):
                propose(bambu_id, local_ip, "pin")
                return
        self._store.update_ip(bambu_id, local_ip)
        if self._fleet.by_id(bambu_id) is not None:
            self._fleet.remove_printer(bambu_id)
        self._fleet.add_printer(PrinterConfig(
            bambu_id=current.bambu_id,
            ip=local_ip,
            access_code=current.access_code,
            name=current.name,
            model=current.model,
        ))
        logger.info("printer %s moved to reserved/current IP %s (was %s)",
                    bambu_id, local_ip, current.ip)
