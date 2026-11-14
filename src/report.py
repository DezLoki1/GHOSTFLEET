"""Write a single readable report.txt for a match folder (result, per-window force status, every assessment, notes, order and refusal,
each model's raw reply, exact prompts excluded). match.py calls this when a match ends; run by hand for older or stopped matches:
python report.py runs/match_<ts>"""
import contextlib, io, json, os, sys

import replay


def write_report(path):
    buf = io.StringIO()
    summ = os.path.join(path, "summary.json")
    if os.path.exists(summ):
        with open(summ, encoding="utf-8") as f:
            buf.write("SUMMARY\n" + json.dumps(json.load(f), indent=2) + "\n\n")
    buf.write("FULL REPLAY (assessments, notes, orders, refusals, raw model replies)\n" + "=" * 70 + "\n")
    with contextlib.redirect_stdout(buf):
        replay.main(path, show_prompts=False, show_raw=True)
    out = os.path.join(path, "report.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write(buf.getvalue())
    return out


if __name__ == "__main__":
    print(write_report(sys.argv[1]))
