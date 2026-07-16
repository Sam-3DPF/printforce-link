"""Reconcile the fleet with the printer config 3DPF couriers down (U4).

On a throttled interval the reconciler pulls `GET /api/bridge/printers/config`, and for
every printer the cloud is delivering a NOT-yet-delivered access code for, it:

  1. writes the code (+ address) to the durable local store (its permanent home), then
  2. adds the printer to the RUNNING fleet if it isn't there and an address is known
     (so a printer added in the web wizard appears without a bridge restart, U2), then
  3. ACKs the delivery so the cloud deletes its copy of the code (courier hand-off done).

Only a printer WITH an access code in the payload is acted on — once a code is delivered
and ACKed the cloud stops sending it, so subsequent pulls list the printer without a
code and it is skipped (already stored). The store, not this pull, is what re-connects
stored printers after a restart (app.py builds the fleet from it at startup).
"""
import logging
import time

from .config import PrinterConfig

logger = logging.getLogger(__name__)

_DEFAULT_RECONCILE_INTERVAL_SECONDS = 60.0


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
        printers = config.get("printers") or []
        acks = []
        for p in printers:
            bambu_id = p.get("bambu_id")
            access_code = p.get("access_code")   # present only while the code is undelivered
            if not bambu_id or not access_code:
                continue
            local_ip = p.get("local_ip")
            # 1. Durably store the code first — the store is its permanent home, so we
            #    must have written it before ACKing the cloud to delete its copy.
            self._store.upsert(bambu_id, access_code, local_ip)
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
                self._fleet.add_printer(
                    PrinterConfig(bambu_id=bambu_id, ip=local_ip, access_code=access_code))
            # 3. Queue the ACK so the cloud deletes the code.
            printer_id, config_version = p.get("printer_id"), p.get("config_version")
            if printer_id and config_version:
                acks.append({"printer_id": printer_id, "config_version": config_version})
        if acks:
            self._dpf.ack_printers_config(acks)
