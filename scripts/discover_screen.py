"""discovery, screening stage: render judge prompts / collect verdicts.

Usage:
    uv run python scripts/discover_screen.py --run <rundir> --round N --render
    uv run python scripts/discover_screen.py --run <rundir> --round N --collect
    uv run python scripts/discover_screen.py --run <rundir> --round N --arbitrate
    uv run python scripts/discover_screen.py --run <rundir> --round N --resolve

--render     ranks the round's pending candidates by concept-term overlap,
             applies the per-round screening cap (overflow marked
             dropped_cap and logged), and writes shuffled batch prompts.
--collect    validates every batch response (ids must match the expected
             list exactly), applies verdicts, and writes any unsure
             candidates to the arbiter queue.
--arbitrate  renders arbiter batch prompts (forced binary) for the round's
             unsure candidates.
--resolve    validates arbiter responses, applies final verdicts, prints
             the round summary and the stopping-rule advice.
"""

import argparse
from pathlib import Path

from scriptorium.discovery import (
    ARBITER_PROMPT,
    _card,
    candidates_path,
    collect_verdicts,
    lex_score,
    load_json,
    protocol_terms,
    render_screen_prompts,
    save_json,
)
from scriptorium.runs import work_dir


def _summary(candidates, round_n) -> str:
    mine = [c for c in candidates if c["round"] == round_n]
    tally = {}
    for c in mine:
        tally[c["status"]] = tally.get(c["status"], 0) + 1
    return ", ".join(f"{k} {v}" for k, v in sorted(tally.items()))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--round", type=int, required=True)
    mode = ap.add_mutually_exclusive_group(required=True)
    for m in ("render", "collect", "arbitrate", "resolve"):
        mode.add_argument(f"--{m}", action="store_true")
    args = ap.parse_args()
    run_dir = Path(args.run).expanduser()
    work = work_dir(run_dir)
    protocol = load_json(run_dir / "protocol.json", None)
    prisma = load_json(run_dir / "prisma.json", None)
    candidates = load_json(candidates_path(run_dir), [])
    mine = [c for c in candidates if c["round"] == args.round]

    if args.render:
        # A re-render replaces the round's batches: stale response files
        # would otherwise attach to the new batch numbering.
        for stale in [*work.glob(f"screen_r{args.round}_batch_*"),
                      *work.glob(f"arbiter_r{args.round}_batch_*")]:
            stale.unlink()
        pending = [c for c in mine if c["status"] == "pending"]
        cap = protocol["caps"]["screen_round_cap"]
        terms = protocol_terms(protocol)
        pending.sort(key=lambda c: -lex_score(c, terms))
        for c in pending[cap:]:
            c["status"] = "dropped_cap"
        kept = pending[:cap]
        n = render_screen_prompts(run_dir, args.round, kept, protocol,
                                  protocol["caps"]["screen_batch"])
        save_json(candidates_path(run_dir), candidates)
        print(f"round {args.round}: {len(kept)} to screen in {n} batches"
              + (f" ({len(pending) - cap} beyond the round cap, dropped and logged)"
                 if len(pending) > cap else ""))
        if len(pending) > cap:
            prisma["rounds"].append({"round": args.round, "stage": "cap",
                                     "dropped": len(pending) - cap})
            save_json(run_dir / "prisma.json", prisma)

    elif args.collect:
        verdicts = collect_verdicts(run_dir, args.round, "screen")
        for c in mine:
            if c["id"] in verdicts:
                c["status"] = verdicts[c["id"]]["verdict"]
                c["why"] = verdicts[c["id"]]["why"]
        save_json(candidates_path(run_dir), candidates)
        unsure = [c for c in mine if c["status"] == "unsure"]
        print(f"round {args.round} screened: {_summary(candidates, args.round)}")
        print("next: --arbitrate" if unsure else "no unsure verdicts — run --resolve")

    elif args.arbitrate:
        unsure = [c for c in mine if c["status"] == "unsure"]
        if not unsure:
            print("nothing to arbitrate")
            return
        include = "\n".join(f"- {c}" for c in protocol["include_criteria"])
        exclude = "\n".join(f"- {c}" for c in protocol["exclude_criteria"])
        batch = protocol["caps"]["screen_batch"]
        n = 0
        for i in range(0, len(unsure), batch):
            chunk = unsure[i:i + batch]
            n += 1
            (work / f"arbiter_r{args.round}_batch_{n}_prompt.txt").write_text(
                ARBITER_PROMPT.format(
                    question=protocol["question"], include=include,
                    exclude=exclude,
                    cards="\n\n".join(_card(c) for c in chunk)))
            save_json(work / f"arbiter_r{args.round}_batch_{n}_expected.json",
                      [c["id"] for c in chunk])
        print(f"round {args.round}: {len(unsure)} unsure in {n} arbiter batches")

    elif args.resolve:
        arb = {}
        if list(work.glob(f"arbiter_r{args.round}_batch_*_expected.json")):
            arb = collect_verdicts(run_dir, args.round, "arbiter")
        for c in mine:
            if c["id"] in arb:
                c["status"] = arb[c["id"]]["verdict"]
                c["why"] = f"arbiter: {arb[c['id']]['why']}"
        still_unsure = [c for c in mine if c["status"] == "unsure"]
        if still_unsure:
            raise SystemExit(
                f"round {args.round}: {len(still_unsure)} candidate(s) still unsure —"
                " run --arbitrate and dispatch the arbiter batches before --resolve")
        save_json(candidates_path(run_dir), candidates)
        includes_now = [c for c in mine if c["status"] == "include"]
        total_includes = [c for c in candidates if c["status"] == "include"]
        # Re-running --resolve replaces this round's row rather than adding one.
        prisma["rounds"] = [r for r in prisma["rounds"]
                            if not (r.get("round") == args.round and r.get("stage") == "screen")]
        prisma["rounds"].append({
            "round": args.round, "stage": "screen",
            "screened": len([c for c in mine
                             if c["status"] not in ("pending", "dropped_cap")]),
            "include": len(includes_now),
            "exclude": len([c for c in mine if c["status"] == "exclude"]),
            "arbitrated": len(arb),
        })
        print(f"round {args.round} final: {_summary(candidates, args.round)}"
              f" · total includes {len(total_includes)}")
        max_rounds = protocol["caps"]["snowball_rounds"]
        if not includes_now and args.round == 0 and protocol["seed_dois"]:
            stop = None  # the seed set is snowballed even when search found nothing
        elif not includes_now:
            stop = "convergence (round added no includes)"
        elif len(total_includes) > len(includes_now) and (
                len(includes_now) / len(total_includes) < 0.01):
            stop = "marginal yield (<1% of accumulated includes)"
        elif args.round >= max_rounds:
            stop = f"round cap ({max_rounds}) reached"
        else:
            stop = None
        if stop:
            prisma["stop"] = stop
            print(f"STOP: {stop} — proceed to verify")
        else:
            print(f"CONTINUE: snowball round {args.round + 1}")
        save_json(run_dir / "prisma.json", prisma)



if __name__ == "__main__":
    main()
