# Emergency RIS + Worklist — design (for review, no code yet)

Status: **proposal**. Covers the two operational use cases and the shared
order model. Nothing here is built beyond the existing HL7-inbound RIS module
(`pacs/ris.py`) — this document is the plan to extend it. Delivery for use
case A is **both** MWL (pull) and HL7-out (push), per decision.

---

## The two use cases

**A — RIS platform is DOWN → Carino is the emergency order *source*.**
A tech hand-keys the patient + order into Carino, and the order must reach the
modalities so the exam can proceed. Carino stands in for the RIS/worklist
originator.

**B — Modality has NO DICOM license → Carino runs on that station as a *gateway*.**
A live RIS sends the order *in* (HL7), it shows on screen, the tech performs the
study in a legacy/non-DICOM program, exports a PDF/image (or prints), and Carino
wraps it as DICOM and forwards it to the real PACS.

Note the asymmetry: **A = order OUT of Carino, RIS absent. B = order INTO Carino,
RIS present.** They share the order store but move data in opposite directions.

---

## The load-bearing insight: modalities PULL, they are not PUSHED

In DICOM you cannot push an order onto a modality. A modality learns its
schedule by **querying a Modality Worklist (MWL)** and pulling matching items.
So "send orders to specific destinations (modalities, AE titles)" becomes:

> Carino runs an **MWL SCP** (worklist provider). Each modality is configured to
> query Carino by AE title and pulls the hand-keyed order, filtered to itself by
> its **Scheduled Station AE Title**. That AE title *is* the "destination".

Consequences:
1. **Hard limit:** a modality that cannot be reconfigured to query Carino as its
   worklist source cannot receive the order — there is no DICOM fallback. This
   must be stated to operators.
2. **HL7-out is the other half of "both":** for destinations that speak HL7 (a
   broker or a second RIS), Carino sends an `ORM^O01` over MLLP. This does *not*
   reach bare modalities — only HL7-capable systems.

---

## Shared order model (the backbone)

Everything revolves around one `OrderStore`. Today an order has:
`accession, patient_id, patient_name, patient, study_desc, modality,
scheduled_dt, referring, priority` + `id/status/source/created/closed`.

For MWL conformance and both use cases, extend with:

| New field | Maps to (DICOM) | Why |
|---|---|---|
| `patient_birthdate` | PatientBirthDate (0010,0030) | MWL patient identification |
| `patient_sex` | PatientSex (0010,0040) | MWL patient identification |
| `station_aet` | ScheduledStationAETitle (0040,0001) | **the "destination" for A**; MWL filter key |
| `station_name` | ScheduledStationName (0040,0010) | optional display |
| `sps_id` | ScheduledProcedureStepID (0040,0009) | MWL procedure step |
| `procedure_id` | RequestedProcedureID (0040,1001) | MWL requested procedure |
| `study_uid` | StudyInstanceUID (0020,000D) | **generated at order creation** so the exam burns the right UID and reconciliation is exact |

`study_uid` generated up front is the linchpin: it makes both the returned-study
match (A) and the wrapped-export identity (B) exact instead of fuzzy.

### Lifecycle

```
                 (MWL C-FIND pulls it)        (study returns / capture bound + sent)
 SCHEDULED ───────────────────────────▶ [IN_PROGRESS?] ─────────────────────────▶ COMPLETED
    │                                        (optional, via MPPS)
    └───────────────────────────────────────────────────────────────────────────▶ CANCELLED
```

Keep the stored `status` as `open | closed` but add `state`
(`scheduled|in_progress|completed|cancelled`) and keep `close_reason`. IN_PROGRESS
is only reachable if we implement MPPS (see Out of scope).

---

## Use case A — the EMERGENCY FAILOVER protocol (revised)

Revised framing: A is not "tell the RIS." The RIS is **dead** and we don't care
what it thinks. A is an **automatic failover state** whose only goal is *keep
imaging flowing and lose nothing*. When the primary system is unreachable too
long, Carino takes over the local roles, and back-fills the real PACS when it
returns.

Operator flow (all manual on the order side):
1. Operator **creates** the worklist order in Carino.
2. Operator **publishes** it → it appears on the modality's worklist (MWL).
3. Modality performs the study → **C-STOREs it back to Carino** (held locally).
4. Operator **relates** the returned study to the order (shared bridge with B)
   and **closes** the order.
5. When the primary PACS is back, the held study is **forwarded** to it.

Two DICOM capabilities underlie this, and **they are independent switches**
(see reach below):

### A1. MWL SCP — `pacs/mwl.py` (NEW module) — the *worklist source*

- `pynetdicom` AE supporting `ModalityWorklistInformationFind` / `EVT_C_FIND`,
  same start/stop/counter/TLS/allowed-AET shape as `StorageSCP` / `PrintSCP`.
- On C-FIND: match **open** orders against the query keys (ScheduledStationAETitle,
  Modality, SPS start-date range, PatientID, AccessionNumber; universal/wildcard),
  yield one worklist item per match (patient level + Scheduled Procedure Step Seq).
- Config `mwl` section; reuses `OrderStore` unchanged — open orders *are* the worklist.

### A2. Hold-and-forward — the *store target* substitute

- Studies C-STORE'd into Carino during the outage are **stored and queued** for
  the primary PACS, then auto-forwarded on recovery.
- **This is mostly the existing machinery**: the folder watcher + stuck-send
  retry with backoff already "keep trying every destination until it accepts,
  nothing dropped." The gap is that *received* studies must **auto-enter the
  forward queue** during emergency (today forwarding a received study is a manual
  Send). So: in emergency mode, a received study is auto-queued to outgoing.

### HL7-out — DECOUPLED / deferred

Sending `ORM` back to the RIS is pointless when the RIS is dead, so HL7-out is
**not part of the emergency protocol**. Keep it as a separate, optional, manual
feature for HL7-capable brokers — or drop it entirely for now. (Open question.)

### Closing an A order

Study returns to Carino → `_reconcile_study` matches (prefer `study_uid`) or the
operator relates it manually → order closes. Reused by B verbatim.

---

## Emergency failover — trigger, state machine, and reach

This is the new piece to "review the logic and reach of." The idea: a
**per-destination toggle** marks the primary PACS; if it is unreachable beyond a
threshold, Carino auto-enters emergency mode.

### The decomposition that keeps it sane

Do **not** treat "emergency" as one monolithic switch. An outage has two
separable failures, and Carino has two separable reactions:

| Primary failure | Carino reaction | Capability |
|---|---|---|
| **Store target down** (PACS won't accept studies) | **Hold & forward** — keep receiving, queue, back-fill on recovery | A2 (mostly exists) |
| **Worklist source down** (RIS/MWL feed gone) | **Local worklist** — MWL SCP + manual order entry | A1 (new) |

In your scenario both fail together (combined RIS+PACS outage) so both fire —
but modelling them separately means a store-only outage doesn't needlessly spin
up a worklist, and a worklist-only outage doesn't imply studies are stranded.
Reach is explicit instead of "everything turns on."

### What counts as "offline" (the detection problem)

A passive signal is not enough: if nothing is being sent, nothing fails, so an
outage that starts during a quiet period is invisible until the next study. So:

- **Active probe:** periodic C-ECHO to each *armed* destination (e.g. every 30 s).
- **Passive signal:** the existing stuck-send failures also count.
- A destination is **offline** after `offline_threshold` of continuous failure
  (probe + real sends agreed), with **hysteresis** on the way back (N consecutive
  successes) so a flapping link can't rapidly toggle emergency on/off. **[decided:
  both signals]**
- **Recovery caveat:** C-ECHO success ≠ C-STORE works. Treat the primary as truly
  back only when a real C-STORE succeeds (or verify before bulk-flushing held
  studies) — otherwise a half-broken node could swallow the backlog.

### Arm, don't surprise-start

Auto-opening listening sockets (MWL/receiver) from a health probe is a big
automatic action. Model it as **arm → trigger**, not silent auto-run:

- Operator **arms** emergency failover once (per destination or globally). This
  is the consent to auto-start servers.
- System **triggers** (enters emergency) when the threshold is crossed, shows a
  loud banner + logs it, and starts the armed reactions (MWL, auto-queue).
- **Exit:** auto-detect recovery and **auto-flush** the held studies, but keep
  MWL running / require a manual **"Resume normal"** to fully stand down — this
  avoids flapping mid-shift. (Confirm preference — see questions.)

### Global emergency state

```
        arm()                threshold crossed              primary verified back
 DISARMED ──▶ ARMED ──────────────────────────▶ ACTIVE ─────────────────────────▶ RECOVERING
                ▲                                   │  (MWL up, receives+queues)      │ flush held
                └───────────── Resume normal ◀──────┴─────────────────────────────────┘  studies
```

State: `armed, active, since, trigger_dest`. A background **health monitor**
thread drives it: probes armed destinations, applies threshold + hysteresis,
enters/exits, emits banner + log events.

### Data-model additions for this

- **Destination** gains: `emergency_trigger` (bool — "this is a primary; watch
  it"), `offline_threshold_sec`, and runtime health (`last_ok, consecutive_fails,
  online`).
- **Global**: the emergency state above.

### Reach — explicit boundaries (what it does NOT do)

- Does **not** auto-create orders — operator creates them (manual only).
- Does **not** auto-close orders without the study relate step.
- Does **not** forward to a destination that only half-recovered (ECHO ok,
  STORE failing).
- Does **not** monitor every destination — only ones flagged `emergency_trigger`.
- Does **not** touch the HL7-inbound path (that's case B; RIS alive).
- Emergency = worklist-out + hold-and-forward. It is a *failover*, not a new
  steady-state mode.

---

## Use case B — order IN, capture OUT (~80% built; one bridge missing)

Already built: HL7 inbound, Orders display, print/ingest→pending pipeline,
destinations + auto-send.

**Missing bridge: bind an exported capture to a displayed order** so the wrapped
DICOM inherits the order's identity instead of being hand-typed.

- From an order: **"Create study from capture"** → pick a PDF/image (or link a
  print job) → ingest stamps the DICOM with the order's
  `patient / patient_id / accession / study_uid / study_desc / birthdate / sex`.
- Print jobs (which carry no identity) get an **"Assign to order"** picker in
  Pending.
- On approve + send → **close the order** (`state=completed`).

Server surface: one method, `create_study_from_order(order_id, file_or_pending_id)`,
reusing `ingest.build_from_bytes` / `save_instance` / `approve_pending`. The rest
is UI.

---

## Reconciliation, unified

An order reaches COMPLETED by one of two routes; matching prefers the strongest
key available:

1. **A route** — study C-STORE'd back → match on `StudyInstanceUID` (exact, now
   that we generate it) → else AccessionNumber → else PatientID.
2. **B route** — capture bound to the order → DICOM generated *with* the order's
   identity → sent → closed on send.

`match_on` stays configurable; add `study_uid` as the top-priority key.

---

## Config surface (net)

- `ris` (exists) — HL7 **inbound** listener. Unchanged.
- `mwl` (new) — worklist **SCP** for modalities.
- `destinations` (exists) — DICOM outbound (studies). Each gains
  `emergency_trigger` + `offline_threshold_sec`.
- `emergency` (new) — global failover config (`armed`, hysteresis counts, probe
  interval, exit behaviour).
- `hl7_destinations` (new, **optional/deferred**) — HL7-out targets. Not part of
  the emergency protocol.
- Order records gain the fields in the earlier table (store schema, not config).

---

## UI / "distribution" (ties to the earlier redesign question)

Service count is now: Receiver, Auto-send, Print, RIS (HL7 in), **MWL (worklist
out)**, **HL7-out** — the flat card row won't scale. Proposed grouping:

- **Inbound**: Receiver · RIS (HL7 in) · Print
- **Outbound**: Auto-send · MWL (worklist) · HL7-out
- **Orders** as the central workspace (schedule, capture, reconcile)

This is the concrete answer to "UI redesign, distribution-wise": group services
by direction, make Orders the hub.

---

## Decisions (locked)

- **Offline signal:** ✅ **both** — active periodic C-ECHO probe *and* passive
  send-failures, with hysteresis on recovery.
- **Enter mode:** ✅ **arm → auto-trigger** — operator arms failover once; system
  auto-enters on threshold with a loud banner.
- **Recovery:** ✅ **auto-flush, manual full exit** — verified recovery auto-forwards
  the held backlog; MWL stays up until the operator clicks "Resume normal".

## Still open (non-blocking)

- **Primary identity:** PACS, RIS, or combined box? The two-switch decomposition
  supports any, so this doesn't block the build — it just sets defaults.
- **HL7-out:** keep for a broker, or drop? Dead weight in the RIS-is-dead
  scenario; scheduled last, buildable or droppable then.
- **MPPS** — deferred; returned-study match + manual "Mark performed" instead.
- **MWL query breadth:** which filter keys do your modalities send? Start with the
  common set (station AET, modality, date, PatientID, accession); widen if needed.

---

## Proposed build order (once approved)

1. ✅ **Done** — Extend the order model (fields + `study_uid` on create) + UID-first matching.
2. ✅ **Done** — B bridge: create-study-from-order + close-on-fulfil. Shared by A and B.
3. ✅ **Done** — A1 MWL SCP (`pacs/mwl.py`): C-FIND worklist provider over the open
   orders, lenient matching, station/modality/date filters, Study-UID carried through.
4. ✅ **Done** — Health monitor + emergency state machine (`pacs/emergency.py`) +
   hold-and-forward. Refinement: no permanent emergency card — a monitored
   destination going offline raises a **pop-up asking to activate** (default;
   `auto_activate` skips it). Worklist/Emergency-RIS cards hidden unless running
   or armed. Banner shows triggered/active/recovering with Activate/Resume.
5. **Next** — UI polish: service regroup (inbound/outbound + Orders hub).
6. **HL7-out** — only if kept (optional, last).
