import re
import requests
import pandas as pd
from pathlib import Path


# ---------- 1) Utility: score_blob parsing ----------
score_pair = re.compile(r"(\d+)\s*-\s*(\d+)")                 # e.g., 2-1
ht_paren   = re.compile(r"\(([^)]+)\)")                        # e.g., (1-0) or (2-2, 2-0)
has_pen    = re.compile(r"\bpen\.?\b", re.I)                   # pen. / PEN.
has_aet    = re.compile(r"\ba\.?e\.?t\.?\b", re.I)             # a.e.t. / aet

def parse_score_blob(blob: str):
    """
    Example blobs:
      - '2-1 (1-0)'
      - '4-2 pen. 3-3 a.e.t. (2-2, 2-0)'
      - '3-0 pen. 0-0 a.e.t. (0-0, 0-0)'
      - '1-3 pen. 1-1 a.e.t (1-1, 1-0)'
    Returns: dict (ft_home/away, aet_home/away, pen_home/away, ht_raw)
    """
    out = {
        "ft_home": None, "ft_away": None,
        "aet_home": None, "aet_away": None,
        "pen_home": None, "pen_away": None,
        "ht_raw": None
    }
    b = blob.strip()

    # Save the original text inside parentheses (first half/additional info)
    m_ht = ht_paren.search(b)
    if m_ht:
        out["ht_raw"] = m_ht.group(1).strip()
        # Exclude parentheses from analysis (to avoid confusion when extracting scores)
        b = ht_paren.sub(" ", b).strip()

    # Check for penalty shootout & extra time
    pen_flag = bool(has_pen.search(b))
    aet_flag = bool(has_aet.search(b))

    # Extract all number pairs and assign interpretation order
    nums = [tuple(map(int, m.groups())) for m in score_pair.finditer(b)]
    # Interpretation rules:
    # - If both pen and aet: [pen score, aet score] is the usual order
    # - Only pen is rare (in World Cup), only aet is possible
    # - If neither, the first is FT

    if pen_flag and len(nums) >= 2:
        out["pen_home"], out["pen_away"] = nums[0]
        out["aet_home"], out["aet_away"] = nums[1]
    elif aet_flag and len(nums) >= 1:
        out["aet_home"], out["aet_away"] = nums[0]
    elif len(nums) >= 1:
        out["ft_home"], out["ft_away"] = nums[0]

    # Supplement: If FT is empty and only a.e.t. exists → also store AET as FT (for summary)
    if out["ft_home"] is None and out["aet_home"] is not None:
        out["ft_home"], out["ft_away"] = out["aet_home"], out["aet_away"]

    return out

# ---------- 2) Match line parsing ----------
# Remove preamble (match number/day/month-day/time) and, from the rest of the string,
# split into [home | score_blob | away @ venue] based on the first score pair position.
pre_head = re.compile(
    r"^\((?P<no>\d+)\)\s+(?P<dow>[A-Za-z]{3})\s+(?P<md>[A-Za-z]{3}/\d{1,2})\s+(?P<time>\d{1,2}:\d{2})\s+"
)

def parse_match_line(line: str):
    m = pre_head.match(line)
    if not m:
        return None
    info = m.groupdict()
    rest = line[m.end():].rstrip()

    # Find venue position (@)
    at_idx = rest.rfind(" @ ")
    if at_idx == -1:
        return None
    before_at = rest[:at_idx].rstrip()
    venue_raw = rest[at_idx + 3:].strip()

    # Find first score position
    m_score = score_pair.search(before_at)
    if not m_score:
        return None
    start = m_score.start()

    home = before_at[:start].strip()
    # Split score_blob and away: use multiple spaces as delimiter, last part is away
    pre_at_tail = before_at[start:].strip()
    parts = re.split(r"\s{2,}", pre_at_tail)  # Usually 2+ spaces between score_blob and team name
    if len(parts) == 1:
        # Defensive: if only one space, treat everything after last score pair as away
        last_sc = list(score_pair.finditer(pre_at_tail))[-1]
        score_blob = pre_at_tail[:last_sc.end()].strip()
        away = pre_at_tail[last_sc.end():].strip()
    else:
        score_blob = " ".join(parts[:-1]).strip()
        away = parts[-1].strip()

    # Split venue into stadium/city (by comma, if present; otherwise only stadium)
    stadium, city = (venue_raw, None)
    if "," in venue_raw:
        stadium, city = [s.strip() for s in venue_raw.split(",", 1)]

    # Parse score_blob
    score = parse_score_blob(score_blob)

    return {
        "match_no": int(info["no"]),
        "dow": info["dow"],
        "month_day": info["md"],
        "time_local": info["time"],
        "home": re.sub(r"\s{2,}", " ", home),
        "away": re.sub(r"\s{2,}", " ", away),
        "stadium": stadium,
        "city": city,
        "score_blob": score_blob,
        **score,
    }

# ---------- 3) Section (group/knockout round) and match parsing ----------
def parse_worldcup_txt(url: str, section_kind="group"):
    """
    section_kind: 'group' -> use group names like 'Group A' as section,
                  'knockout' -> use round names like 'Round of 16', 'Quarter-finals' as section
    """
    r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    lines = r.text.splitlines()

    sec_label = None
    out = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue

        # Section header
        if section_kind == "group" and line.startswith("Group "):
            # Example: "Group A"
            sec_label = line.split()[0] + " " + line.split()[1]  # "Group A"
            continue
        if section_kind == "knockout" and line in {
            "Round of 16", "Quarter-finals", "Semi-finals",
            "Match for third place", "Final"
        }:
            sec_label = line
            continue

        # Try match line
        row = parse_match_line(line)
        if row:
            row["section"] = sec_label
            row["stage"] = "Group" if section_kind == "group" else "Knockout"
            out.append(row)

    return pd.DataFrame(out)
