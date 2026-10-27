"""Model profiles and switching (master prompt section 6, resource rule 4).

A profile is a generation model plus its context size and declared capabilities. The 'default' profile is the
[ollama] section as configured at startup; [[models.profiles]] add more. The active profile is applied to the
in-memory [ollama] settings, which the provider, chat and tasks all read, so a switch needs no change anywhere
else. Nothing is written to the configuration files: a restart returns to the default profile.

Switching follows resource rule 4: refuse while anything is generating; unload the current model (keep_alive 0) and
wait until Ollama has dropped it; confirm free VRAM against the target's measured footprint; load; warm up. The
single model slot of the admission gate is held for the whole switch, so no chat or task can call the model
half-way through. XODUS never downloads a model: a profile whose model is not installed cannot be switched to, and
neither can one whose model Ollama would run on another computer.
Every attempt, including refusals, is recorded in model_switches and the audit log.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any

from xodus.config.settings import OllamaSettings, Settings
from xodus.obs import log_event
from xodus.providers.base import ModelProvider, ProviderError, RequestBudget
from xodus.providers.ollama import find_model, is_remote_model, remote_model_error
from xodus.resources.admission import AdmissionGate, QueueFull
from xodus.resources.sampler import Nvml, mib
from xodus.security.audit import AuditLog
from xodus.storage.sqlite import Database

DEFAULT = "default"


@dataclass(frozen=True)
class Profile:
    id: str
    label: str
    model: str
    expected_digest_prefix: str | None
    capabilities: tuple[str, ...]
    num_ctx: int
    enabled: bool
    note: str


def build_profiles(settings: Settings) -> dict[str, Profile]:
    o = settings.ollama
    profiles = {DEFAULT: Profile(DEFAULT, "Default", o.model, o.expected_digest_prefix, tuple(o.capabilities), o.num_ctx, True,
                                 "The [ollama] section of the configuration.")}
    for p in settings.models.profiles:
        profiles[p.id] = Profile(p.id, p.label or p.id, p.model, p.expected_digest_prefix, tuple(p.capabilities), p.num_ctx,
                                 p.enabled, p.note)
    return profiles


def apply_profile(ollama: OllamaSettings, profile: Profile) -> None:
    """Make `profile` the active generation model for everything that reads the [ollama] settings."""
    ollama.model = profile.model
    ollama.expected_digest_prefix = profile.expected_digest_prefix
    ollama.capabilities = list(profile.capabilities)  # type: ignore[assignment]
    ollama.num_ctx = profile.num_ctx


class SwitchError(Exception):
    def __init__(self, code: str, message: str, status: int = 409, record: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.record = record


class SwitchLog:
    def __init__(self, db: Database) -> None:
        self.db = db

    def record(self, **row: Any) -> dict[str, Any]:
        cols = list(row)
        with self.db.transaction() as conn:
            cur = conn.execute(f"INSERT INTO model_switches({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                               [row[c] for c in cols])
            return dict(conn.execute("SELECT * FROM model_switches WHERE id = ?", (cur.lastrowid,)).fetchone())

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.db.connection() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM model_switches ORDER BY id DESC LIMIT ?", (max(1, min(limit, 200)),))]

    def footprints(self) -> dict[str, dict[str, Any]]:
        """Latest measured device-VRAM increase per profile (load and warm-up), from successful switches."""
        out: dict[str, dict[str, Any]] = {}
        with self.db.connection() as conn:
            for r in conn.execute("SELECT to_profile, vram_after_unload, vram_after_load, num_ctx, started_at FROM model_switches"
                                  " WHERE outcome = 'switched' AND vram_after_load IS NOT NULL AND vram_after_unload IS NOT NULL"
                                  " ORDER BY id"):
                out[r["to_profile"]] = {"vram_bytes": max(0, r["vram_after_load"] - r["vram_after_unload"]),
                                        "num_ctx": r["num_ctx"], "measured_at": r["started_at"]}
        return out


class ModelManager:
    def __init__(self, settings: Settings, provider: ModelProvider, admission: AdmissionGate, nvml: Nvml, log: SwitchLog,
                 audit: AuditLog, busy: Callable[[], str | None],
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.settings = settings
        self.provider = provider
        self.admission = admission
        self.nvml = nvml
        self.log = log
        self.audit = audit
        self.busy = busy
        self.profiles = build_profiles(settings)
        self.active_id = DEFAULT
        self.switching: str | None = None
        self._lock = asyncio.Lock()
        self._sleep = sleep

    @property
    def active(self) -> Profile:
        return self.profiles[self.active_id]

    def _apply(self, profile: Profile) -> None:
        apply_profile(self.settings.ollama, profile)
        self.active_id = profile.id

    async def describe(self) -> dict[str, Any]:
        installed: dict[str, dict[str, Any]] | None
        try:
            installed = {}
            for m in await self.provider.list_models():
                for name in {m.get("name"), m.get("model")} - {None}:
                    installed[str(name)] = m
        except ProviderError:
            installed = None  # Ollama unreachable: installation state unknown
        running = {m.get("name") or m.get("model"): m for m in await self.provider.running_models()}
        footprints = await asyncio.to_thread(self.log.footprints)
        rows = []
        for p in self.profiles.values():
            found = installed.get(p.model) if installed is not None else None
            digest = found.get("digest") if found else None
            loaded = running.get(p.model)
            rows.append({
                **asdict(p), "capabilities": list(p.capabilities), "active": p.id == self.active_id,
                "installed": None if installed is None else found is not None,
                "remote": is_remote_model(found) if found else None,
                "digest": digest,
                "digest_ok": None if not (digest and p.expected_digest_prefix) else digest.startswith(p.expected_digest_prefix),
                "size_bytes": found.get("size") if found else None,
                "loaded": loaded is not None,
                "loaded_size_vram": loaded.get("size_vram") if loaded else None,
                "measured_vram_mib": mib(footprints[p.id]["vram_bytes"]) if p.id in footprints else None,
                "measured_at": footprints[p.id]["measured_at"] if p.id in footprints else None,
                "measured_num_ctx": footprints[p.id]["num_ctx"] if p.id in footprints else None,
            })
        vram = self.nvml.memory()
        return {"active": self.active_id, "switching": self.switching, "profiles": rows,
                "running": [{"name": n, "size": m.get("size"), "size_vram": m.get("size_vram"), "expires_at": m.get("expires_at")}
                            for n, m in running.items()],
                "embed_model": self.settings.ollama.embed_model, "ollama_reachable": installed is not None,
                "device_vram": {k: mib(v) for k, v in vram.items()} if vram else None,
                "recent_switches": await asyncio.to_thread(self.log.recent, 10)}

    async def reconcile(self) -> list[str]:
        """At startup: unload generation models left loaded by an earlier run (never two generation models resident)."""
        unloaded = []
        keep = {self.active.model, self.settings.ollama.embed_model}
        generators = {p.model for p in self.profiles.values()}
        for m in await self.provider.running_models():
            name = m.get("name") or m.get("model")
            if name in generators and name not in keep:
                try:
                    await self._unload_model(name)
                    unloaded.append(name)
                except ProviderError as exc:
                    log_event("model_reconcile_failed", model=name, message=exc.message)
        if unloaded:
            self.audit.append("model_unloaded_at_startup", models=unloaded)
        return unloaded

    async def _unload_model(self, name: str) -> None:
        current = self.settings.ollama.model
        self.settings.ollama.model = name
        try:
            await self.provider.unload()
        finally:
            self.settings.ollama.model = current

    def _vram(self) -> dict[str, int] | None:
        return self.nvml.memory()

    async def switch(self, profile_id: str) -> dict[str, Any]:
        if self._lock.locked():
            raise SwitchError("switch_in_progress", "Another model switch is in progress.")
        async with self._lock:
            target = self.profiles.get(profile_id)
            if target is None:
                raise SwitchError("unknown_profile", f"No profile named {profile_id!r}.", 404)
            previous = self.active
            started, t0 = time.time(), time.perf_counter()
            base = {"from_profile": previous.id, "to_profile": target.id, "from_model": previous.model,
                    "to_model": target.model, "num_ctx": target.num_ctx, "started_at": started}

            async def refuse(code: str, message: str, status: int = 409, **vram: Any) -> SwitchError:
                row = await asyncio.to_thread(self.log.record, **base, **vram, outcome="refused", code=code, message=message,
                                              duration_ms=round((time.perf_counter() - t0) * 1000, 1))
                self.audit.append("model_switch_refused", **{k: base[k] for k in ("from_profile", "to_profile", "to_model")},
                                  code=code, message=message)
                return SwitchError(code, message, status, row)

            if target.id == previous.id:
                raise await refuse("already_active", f"{target.label} is already the active profile.")
            if not target.enabled:
                raise await refuse("profile_disabled", f"{target.label} is disabled in the configuration: {target.note}")
            reason = self.busy()
            if reason:
                raise await refuse("busy", reason)
            try:
                models = await self.provider.list_models()
            except ProviderError as exc:
                raise await refuse("provider_unavailable", exc.message, 503) from exc
            found = find_model(models, target.model)
            if found is None:
                raise await refuse("not_installed", f"{target.model} is not installed in Ollama. XODUS never downloads models; "
                                                    f"to use this profile run: ollama pull {target.model}")
            if is_remote_model(found):
                raise await refuse("remote_model", remote_model_error(target.model, found.get("remote_host") or None).message)
            digest = found.get("digest") or ""
            if target.expected_digest_prefix and not digest.startswith(target.expected_digest_prefix):
                raise await refuse("digest_mismatch", f"Installed digest {digest[:12]} does not match the pinned "
                                                      f"{target.expected_digest_prefix}.")
            try:
                slot = self.admission.slot()
                await slot.__aenter__()
            except QueueFull as exc:
                raise await refuse("busy", str(exc)) from exc
            self.switching = target.id
            vram_before = self._vram()
            try:
                return await self._switch_locked(target, previous, base, t0, vram_before, refuse)
            finally:
                self.switching = None
                await slot.__aexit__(None, None, None)

    async def _switch_locked(self, target: Profile, previous: Profile, base: dict[str, Any], t0: float,
                             vram_before: dict[str, int] | None, refuse: Callable[..., Awaitable[SwitchError]]) -> dict[str, Any]:
        cfg = self.settings.models
        before = vram_before["used"] if vram_before else None
        try:
            await self.provider.unload()
            deadline = time.monotonic() + cfg.unload_wait_s
            while time.monotonic() < deadline:
                if previous.model not in {m.get("name") or m.get("model") for m in await self.provider.running_models()}:
                    break
                await self._sleep(0.25)
            else:
                raise await refuse("unload_timeout", f"Ollama still had {previous.model} loaded after {cfg.unload_wait_s:.0f} s; "
                                                     "the switch was not attempted.", vram_before=before)
        except ProviderError as exc:
            raise await refuse("provider_unavailable", exc.message, 503, vram_before=before) from exc
        after_unload = self._vram()
        unloaded = after_unload["used"] if after_unload else None
        need = (await asyncio.to_thread(self.log.footprints)).get(target.id)
        if need and after_unload and after_unload["free"] < need["vram_bytes"]:
            raise await refuse(
                "insufficient_vram",
                f"Only {mib(after_unload['free']):.0f} MiB of VRAM is free; {target.label} was measured to need about "
                f"{mib(need['vram_bytes']):.0f} MiB. {previous.label} stays active (it reloads on the next request).",
                vram_before=before, vram_after_unload=unloaded)
        self._apply(target)
        try:
            info = await self.provider.probe()
            missing = sorted(set(target.capabilities) - set(info.confirmed))
            if "completion" in missing:
                raise ProviderError(f"{target.model} does not confirm the 'completion' capability.")
            await self.provider.load(num_ctx=target.num_ctx)
            if cfg.warmup:
                await self.provider.generate([{"role": "user", "content": "Reply with OK."}],
                                             RequestBudget(num_ctx=target.num_ctx, max_output_tokens=8))
        except ProviderError as exc:
            self._apply(previous)
            try:
                await self.provider.probe()
            except ProviderError:
                pass
            row = await asyncio.to_thread(
                self.log.record, **base, outcome="failed", code="load_failed", message=exc.message, vram_before=before,
                vram_after_unload=unloaded, duration_ms=round((time.perf_counter() - t0) * 1000, 1))
            self.audit.append("model_switch_failed", from_profile=previous.id, to_profile=target.id, message=exc.message)
            raise SwitchError("load_failed", f"Loading {target.model} failed: {exc.message} {previous.label} stays active.",
                              502, row) from exc
        after_load = self._vram()
        loaded = after_load["used"] if after_load else None
        duration = round((time.perf_counter() - t0) * 1000, 1)
        row = await asyncio.to_thread(self.log.record, **base, outcome="switched", vram_before=before,
                                      vram_after_unload=unloaded, vram_after_load=loaded, duration_ms=duration)
        self.audit.append("model_switched", from_profile=previous.id, to_profile=target.id, model=target.model,
                          num_ctx=target.num_ctx, duration_ms=duration, vram_after_unload_mib=mib(unloaded),
                          vram_after_load_mib=mib(loaded), unconfirmed_capabilities=missing)
        log_event("model_switched", **{k: base[k] for k in ("from_profile", "to_profile", "to_model", "num_ctx")},
                  duration_ms=duration)
        return {**row, "unconfirmed_capabilities": missing,
                "footprint_mib": mib(loaded - unloaded) if loaded is not None and unloaded is not None else None}
