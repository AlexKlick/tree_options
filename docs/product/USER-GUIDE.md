# TREX user guide

TREX is a private paper trading desk for its owner. Three rules hold on every
page:

- **Paper only.** TREX trades an IBKR paper account (account ids that start
  with `D`). There is no live path, and paper acceptance never unlocks live
  trading.
- **You approve every entry.** The desk proposes; nothing opens until you
  approve it. The app never approves anything for you.
- **Exits are automatic.** Once a position is open, the desk closes it by its
  stop, its target, its deadline or the forced exit before the close. You do
  not approve exits, and pausing entries never stops them.

## Where things are

Five destinations, on the desktop and on the phone (390 x 844 and up):

| Destination | Phone tab | What it is for |
| --- | --- | --- |
| Today | Today | what needs you now: approvals with their countdown, the desk and gateway state, the stops |
| Experiments | Experiments | research evidence and the research pilot |
| Trade review | Review | one proposal in full before you approve or reject it; ideas and a manual SPY ticket to start one |
| Portfolio | Portfolio | holdings, cash, reconciliation, positions outside TREX, the risk snapshot |
| Activity & settings | Activity | the activity log, paper acceptance progress, settings |

The control bar with the stops is at the top of every page. Getting started
(`#/onboarding`) is the first-run checklist.

## Getting started

Open Getting started (`#/onboarding`). It reads Today once and checks five
things:

1. **IB Gateway logged in**: the gateway watch reports the gateway answering.
2. **Desk owner heartbeat**: the desk owner holds its lock and its heartbeat
   is fresh, so open positions have a watching exit owner.
3. **Capital profile present**: the desk has a valid capital profile. Today
   can confirm it only through a proposal, so before the first proposal this
   step may read Unknown.
4. **Family command key installed**: approvals from the phone reach the desk.
   Today has no positive fact for this, so the step reads **Unknown**, or
   **To do** when Today reports "Commands from Family Apps are turned off".
   It never reads Done: the copy of the key on the Family Apps portal cannot
   be seen from TREX. Your first approval from the phone settles it: Activity
   then lists it "via Family Apps (phone or desktop)".
5. **First paper approval**: TREX holds a desk position you approved, or paper
   acceptance counts a completed paper trade.

Each step reads **Done**, **To do** or **Unknown**. Unknown means Today could
not show that fact; the step says why. Unknown is never counted as done.

## Approve a trade from your phone

1. Open TREX through the Family Apps portal (the TREX app, under `/trex/`) and
   sign in as the owner. The portal signs each command you send for this one
   request; nothing else can send commands from outside this computer.
2. Today lists the proposals waiting for you. Each has a countdown: the time
   left before the proposal's send deadline. At zero the proposal expires and
   nothing is sent.
3. Open the proposal (Trade review). Check what it buys or sells, the limit
   price, the stop and the target, the most it can lose, the notional, and any
   warnings. The warnings are advisory; the desk checks everything again
   before it sends.
4. Approve it, or reject it. Your phone sends one command. It is queued, and
   the desk applies it.
5. Watch Activity: the command reads "Queued, not applied yet", then "Applied
   by the desk", or "Refused" with the reason in words. After a fill the
   position appears in Portfolio, and its exits run on their own.

When an approval does not go through:

| You see | Why | What to do |
| --- | --- | --- |
| The desk restarted after the proposal was made (`approval_epoch_mismatch`) | a desk restart invalidates every waiting approval | wait for a fresh proposal and approve that |
| The approval ran out of time (`approval_expired`) | an approval lives at most 300 seconds and never past the proposal's deadline | approve sooner next time |
| The command expired before the desk applied it (`command_expired`) | the desk did not apply it within 300 seconds | check the desk owner heartbeat on Today |
| Duplicate (first outcome kept) | you sent the same approval twice | nothing: sending twice is safe |
| The command was not authorized (`command_unauthorized`), or the portal answers `trex-commands-not-configured` | the command key is missing, or differs, on the Family Apps portal or on TREX | on the host, install the same key file on both sides (`~/.config/family-apps/trex-command.key` for the portal, `~/.config/trex/family-command.key` for TREX); see Getting started, step 4 |

## Start a trade yourself

Trade review (the Review tab) also lets you start an entry. Starting one only
asks the desk to prepare a proposal: nothing is approved or sent until you
approve that proposal as above.

- **Ideas** lists the candidates the desk qualified, each with its max loss,
  notional and the time left to use it. **Prepare proposal** sends one
  request; a blocked, expired or already used idea says why and has no
  button, and an idea that already has a waiting proposal links to it.
- Warnings such as "New entries are paused" or a closed entry window are
  advisory: the page shows them in words, and the desk decides when it
  prepares the proposal (a refusal shows its reason).
- **New SPY trade** is a manual ticket for whole SPY shares: the number of
  shares, how the entry limit is set (pay the ask, the midpoint, or your own
  limit), a stop below the entry price, an optional target above it, and the
  last market session to hold the position. The page checks these before it
  sends; the desk checks everything again with its real-time quote.
- The ticket is paper only and cash only (no margin, no short selling); the
  desk refuses an SPY entry on a delayed quote and prepares entries only
  during regular market hours.
- When the desk has prepared the proposal, **Review and approve the proposal**
  opens it; approve it there before its countdown runs out. If the request was
  not confirmed, **Try again (same request)** re-sends the same request, so it
  can never prepare two proposals from one press.

## Pause new entries or stop everything

The stops are in the control bar at the top of every page. Settings
(`#/activity/settings`) explains them and shows which ones stand; it does not
repeat them.

| Control | What it does | Exits |
| --- | --- | --- |
| Pause new entries | The primary control. The desk opens no new positions. | keep running |
| Resume new entries | The desk may open new positions again; each still needs your approval. | keep running |
| Emergency stop (also stops exits) | Stops everything. Open positions are not watched until you clear it. | **stopped** |
| Close all desk positions | Closes every position the desk owns, options and stocks, and blocks new entries while it stands. Never touches positions outside TREX. | closing |
| Clear emergency stop, close-all and pause | Removes all three stops. It does not bring back earlier approvals, and it never lifts a stop sent after it. | resume |

Which one to use:

- **Unsure, worried, away from the screen: Pause new entries.** Nothing new
  opens, and every open position keeps its exits.
- **The desk itself misbehaves** (for example it acts on something you did
  not approve): Emergency stop (also stops exits). Then decide, with the
  positions in front of you, whether to close them. While it stands nobody
  watches the open positions, and for paper acceptance a standing emergency
  stop counts as an unwatched exit.
- **You want out of every desk position:** Close all desk positions.

## Sunday 2FA

IBKR asks for a full login with two-factor approval once a week. The IB
Gateway is set to do that weekly restart on **Sunday at 12:00 ET**, so the
prompt comes at a sociable hour and outside market hours.

1. Around Sunday noon ET, IBKR Mobile shows a login prompt. Approve it.
2. Today's gateway line moves from "IB Gateway is waiting for 2FA"
   (`needs_2fa`) to logged in within a minute or two.
3. If you miss the prompt, the gateway asks again instead of stalling. The
   gateway watch also alerts when the gateway enters a bad state, every 4
   hours while it lasts, and when it recovers.
4. If it still is not logged in, log in by hand as the gateway runbook
   (`docs/trex-gateway-runbook.md`) describes: open the gateway screen from a
   device on the tailnet, choose Paper Trading, log in and approve 2FA. The
   screen password lives on the host; read it there and never paste it into a
   chat.

While the gateway is logged out the desk cannot reach the broker: no new
entries, and open positions have no exit machine. Finish the login before
Monday's open. The daily gateway restart keeps the session and needs no 2FA.

## What 'unavailable' means

TREX shows **Unavailable** when it cannot read a fact: a file is missing or
unreadable, a watch stopped reporting, a record has not been computed yet. It
always gives the reason in words with the machine code in brackets, for
example "Unavailable: No risk snapshot has been written yet
(risk_snapshot_missing)".

- **Unavailable is never zero.** An unavailable cash balance is not $0.00, an
  unavailable holdings list is not "no positions", and an unavailable activity
  source is not an empty log.
- **Unknown is never healthy or done.** Getting started shows Unknown rather
  than Done, and paper acceptance never counts an unrecorded session.
- **Stale is different.** A stale figure is shown with its age and marked
  Stale. New entries are blocked while the risk snapshot or the reconciliation
  is stale; exits continue.

Reasons you are most likely to meet:

| Code | Meaning | What to do |
| --- | --- | --- |
| `heartbeat_absent` | the desk owner has written no heartbeat | check the desk service on the host |
| `gateway_watch_not_reporting` | the gateway watch is silent, so gateway health is unknown | check the gateway watch on the host |
| `holdings_missing` | the books have not written holdings yet | normal before the first run; otherwise check the desk |
| `risk_snapshot_missing`, `risk_snapshot_stale` | no fresh broker pricing of the account | usually the gateway; entries wait for a fresh snapshot |
| `reconciliation_required` | TREX's books and the broker disagree | Portfolio lists each mismatched contract; entries stay blocked until they match |
| `kill_file_unreadable` | Today cannot tell whether a stop stands | check the desk run directory on the host |
| `acceptance_missing` | paper acceptance has not been computed yet | it appears once the acceptance job has run |
| `activity_source_unreadable` | one activity source could not be read | its items are missing from the log, not absent |
| `actor_not_recorded` | an older record does not say who acted | nothing |

## Paper acceptance progress

Open Activity & settings, then Paper acceptance (`#/activity/acceptance`).
Today shows a summary. Everything is computed from the records and checked
again on every read; nobody can type a result in.

The paper phase is accepted when all three hold:

1. **10 consecutive qualifying sessions.** A regular NYSE session qualifies
   when:
   - the desk owner's heartbeat was fresh (at most 120 seconds old) for at
     least 99% of regular hours (387 of 390 minutes; 208 of 210 on an early
     close);
   - the books matched the broker at the close, every mismatch was resolved
     by the close, and a mismatch at the open was taken up within 15 minutes;
   - the broker had no order or execution that TREX does not own;
   - no open desk position went unwatched for more than 300 seconds (a
     standing Emergency stop (also stops exits) counts as unwatched).

   A session that does not qualify, or was not recorded, starts the streak
   again. Unknown never qualifies.
2. **10 completed paper trades of each kind** (options and stocks) inside the
   current streak: filled, exited by policy or by Close all desk positions,
   flat at the broker, reconciled clean, with commissions for every fill.
3. **All 7 drills passed**, each with evidence and attested by an owner:
   - Log back in after a gateway logout
   - Restart the desk with a position open
   - Ride out a gateway outage
   - Send the same command twice
   - Back up and restore the desk
   - Receive a problem alert
   - Use TREX without help

A drill that breaks a session rule also resets the streak, so plan drills
around it (a restart must keep the heartbeat gap under about 3 minutes).

When everything holds the page says **Met**. Even then, paper acceptance never
unlocks live trading: live trading would need separately configured limits,
account qualification and an explicit activation, and none of that exists in
this version.

## The activity log

Activity (`#/activity`) lists, newest first, the commands you sent, the
desk's own events, audit records and drills. Each line says who acted and
through which door: "via Family Apps (phone or desktop)", "via this computer
(loopback)" or "via the TREX desk (automatic)". Filter by source with the
buttons above the list; "Load older activity" pages back. A source TREX cannot
read is listed as unavailable above the log.

## Portfolio and settings

Portfolio (`#/portfolio`) shows what TREX's books hold (and which part of a
position is outside TREX), settled cash at the broker beside the cash in the
books, whether the books match the broker (with each mismatch), broker
positions outside TREX (which TREX never trades or closes), and the age of the
risk snapshot. It is read-only.

Settings (`#/activity/settings`) shows the paper account, what Today can tell
about the capital profile, the exit-watch and command time limits, what Today
can tell about phone commands, the stops' current state, and the acceptance
rules. It
changes nothing; the owner changes the profile on the host.
