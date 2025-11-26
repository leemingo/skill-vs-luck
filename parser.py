import datetime as dt
import re
import requests
import pandas as pd
from pathlib import Path
from typing import Optional

# ---------- 1) Utility: score_blob parsing ----------
score_pair = re.compile(r"(\d+)\s*-\s*(\d+)")                 # e.g., 2-1
ht_paren   = re.compile(r"\(([^)]+)\)")                        # e.g., (1-0) or (2-2, 2-0)
has_pen    = re.compile(r"\bpen\.?\b", re.I)                   # pen. / PEN.
has_aet    = re.compile(r"\ba\.?e\.?t\.?\b", re.I)             # a.e.t. / aet

MONTH_MAP = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


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

def parse_champions_league_txt_file(file_path: Path, season: str) -> pd.DataFrame:
    """
    Champions League 텍스트 파일을 파싱하여 DataFrame으로 변환
    
    Parameters:
    -----------
    file_path : Path
        cl.txt 파일 경로
    season : str
        시즌 (예: "2011-12")
    
    Returns:
    --------
    pd.DataFrame
        컬럼: season, date, time, stage, section, home_team, away_team,
              home_goals, away_goals, ht_home, ht_away, has_penalty, has_aet
    """
    rows = []
    start_year = int(season.split("-")[0])
    
    current_date: Optional[dt.date] = None
    current_stage: Optional[str] = None
    current_section: Optional[str] = None
    
    with file_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.rstrip()
            
            # 빈 줄 건너뛰기
            if not line:
                continue
            
            # 헤더 라인 건너뛰기 (= 로 시작하거나 # 로 시작)
            if line.startswith("=") or line.startswith("#"):
                continue
            
            # 그룹/라운드 헤더 (» 로 시작)
            if line.startswith("»"):
                current_section = line.replace("»", "").strip()
                # Stage 분류
                if "Group" in current_section:
                    current_stage = "Group Stage"
                elif "Round of 16" in current_section or "Round of" in current_section:
                    current_stage = "Round of 16"
                elif "Quarter" in current_section or "Quarterfinals" in current_section:
                    current_stage = "Quarterfinals"
                elif "Semi" in current_section or "Semifinals" in current_section:
                    current_stage = "Semifinals"
                elif "Final" in current_section:
                    current_stage = "Final"
                else:
                    current_stage = "Other"
                continue
            
            # 날짜 라인 (요일 + 월/일 형식)
            # 예: "  Wed Sep/14 2011" 또는 "  Tue Sep/27"
            date_match = re.match(r"^\s+(\w+)\s+(\w+)/(\d{1,2})(?:\s+(\d{4}))?", line)
            if date_match:
                weekday, mon_str, day_str, year_str = date_match.groups()
                
                if mon_str not in MONTH_MAP:
                    continue
                
                month = MONTH_MAP[mon_str]
                day = int(day_str)
                
                # 연도 처리: 명시되어 있으면 사용, 없으면 시즌 시작 연도 사용
                if year_str:
                    year = int(year_str)
                else:
                    # 7월 이전은 다음 해로 간주 (시즌이 다음 해까지 이어지므로)
                    year = start_year + 1 if month < 7 else start_year
                
                current_date = dt.date(year, month, day)
                continue
            
            # 경기 라인 파싱
            # 형식: "    20.45  Team A (COUNTRY)   v Team B (COUNTRY)         1-1 (0-0)"
            # 또는: "           Team A (COUNTRY)   v Team B (COUNTRY)         1-1 (0-0)"
            match_line = re.match(
                r"^\s+(?:(\d{1,2}\.\d{2})\s+)?(.+?)\s+v\s+(.+?)\s+(\d+)-(\d+)(?:\s+\((\d+)-(\d+)\))?(?:\s+(.+))?$",
                line
            )
            
            if match_line and current_date is not None:
                time_str, home_part, away_part, home_goals, away_goals, ht_home, ht_away, extra = match_line.groups()
                
                # 팀 이름에서 국가 코드 제거 (괄호 안의 내용)
                home_team = re.sub(r"\s*\([^)]+\)\s*$", "", home_part).strip()
                away_team = re.sub(r"\s*\([^)]+\)\s*$", "", away_part).strip()
                
                # 승부차기/연장전 확인
                has_penalty = False
                has_aet = False
                if extra:
                    has_penalty = bool(re.search(r"\bpen\.?\b", extra, re.I))
                    has_aet = bool(re.search(r"\ba\.?e\.?t\.?\b", extra, re.I))
                
                rows.append({
                    "season": season,
                    "date": current_date.isoformat(),
                    "time": time_str if time_str else None,
                    "stage": current_stage,
                    "section": current_section,
                    "home_team": home_team,
                    "away_team": away_team,
                    "home_goals": int(home_goals),
                    "away_goals": int(away_goals),
                    "ht_home": int(ht_home) if ht_home else None,
                    "ht_away": int(ht_away) if ht_away else None,
                    "has_penalty": has_penalty,
                    "has_aet": has_aet,
                    "source_file": file_path.name
                })
    
    return pd.DataFrame(rows)


def parse_champions_league_directory(
    root: str | Path,
    min_season: str | None = None
) -> pd.DataFrame:
    """
    Champions League 디렉토리에서 모든 시즌 데이터를 파싱
    
    Parameters:
    -----------
    root : str | Path
        champions-league 디렉토리 경로
    min_season : str, optional
        최소 시즌 (예: "2010-11")
    
    Returns:
    --------
    pd.DataFrame
        모든 시즌의 경기 데이터
    """
    root = Path(root)
    all_dfs = []
    
    for season_dir in sorted(root.iterdir()):
        if not season_dir.is_dir():
            continue
        
        # 시즌 디렉토리 이름 형식 확인 (YYYY-YY)
        if not re.match(r"^\d{4}-\d{2}$", season_dir.name):
            continue
        
        season = season_dir.name
        
        if min_season is not None and season < min_season:
            continue
        
        # cl.txt 파일 찾기
        cl_file = season_dir / "cl.txt"
        if cl_file.exists():
            df = parse_champions_league_txt_file(cl_file, season)
            all_dfs.append(df)
            print(f"✓ Parsed {season}: {len(df)} matches")
        else:
            print(f"⚠ No cl.txt found in {season}")
    
    if not all_dfs:
        return pd.DataFrame()
    
    result_df = pd.concat(all_dfs, ignore_index=True)
    return result_df

