#!/usr/bin/env python3
"""Read-only, standard-library lookup for the bundled reviewed knowledge.

This does not call the deployed school MCP or write to a database. Only public,
published, reviewed cards are exposed. The data path is fixed for CLI callers;
KnowledgeStore(path) is available to trusted local integrators and tests.
"""
from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import json
import math
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

DEFAULT_DATA = Path(__file__).resolve().parent.parent / "references" / "knowledge-cards.json"
MAX_QUERY = 400
MAX_CARDS = 20000
MAX_DATA_BYTES = 24 * 1024 * 1024
MAX_LIMIT = 10
PUBLIC_FIELDS = (
    "id", "title", "kind", "topic", "countries", "levels", "tags", "summary",
    "content", "applies_when", "not_applicable_when", "next_steps", "source_label",
    "verification_status", "as_of", "last_verified_at", "valid_until",
    "review_due_at", "effective_from", "official_urls",
)
LIST_FIELDS = {"countries", "levels", "tags", "applies_when", "not_applicable_when", "next_steps", "official_urls"}
DATE_FIELDS = {"as_of", "last_verified_at", "valid_until", "review_due_at", "effective_from"}
ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,95}\Z")
PRIVATE_TEXT_RE = re.compile(r"(?:file://|/(?:Users|home|workspace|private|var)/|[A-Za-z]:\\|(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=])", re.I)
COUNTRIES = {
    "sg": "SG", "singapore": "SG", "新加坡": "SG",
    "my": "MY", "malaysia": "MY", "马来西亚": "MY", "大马": "MY",
    "hk": "HK", "hong kong": "HK", "香港": "HK", "中国香港": "HK",
    "mo": "MO", "macau": "MO", "澳门": "MO", "中国澳门": "MO",
    "uk": "UK", "gb": "UK", "united kingdom": "UK", "英国": "UK",
    "us": "US", "usa": "US", "united states": "US", "美国": "US",
    "au": "AU", "australia": "AU", "澳洲": "AU", "澳大利亚": "AU",
    "ca": "CA", "canada": "CA", "加拿大": "CA",
    "ie": "IE", "ireland": "IE", "爱尔兰": "IE",
    "nz": "NZ", "new zealand": "NZ", "新西兰": "NZ",
    "cn": "CN", "china": "CN", "中国": "CN", "中国大陆": "CN",
    "ch": "CH", "switzerland": "CH", "瑞士": "CH",
    "de": "DE", "germany": "DE", "德国": "DE",
    "fr": "FR", "france": "FR", "法国": "FR",
    "nl": "NL", "netherlands": "NL", "荷兰": "NL",
    "be": "BE", "belgium": "BE", "比利时": "BE",
    "es": "ES", "spain": "ES", "西班牙": "ES",
    "it": "IT", "italy": "IT", "意大利": "IT",
    "jp": "JP", "japan": "JP", "日本": "JP",
    "kr": "KR", "korea": "KR", "south korea": "KR", "韩国": "KR",
    "th": "TH", "thailand": "TH", "泰国": "TH",
    "ae": "AE", "uae": "AE", "united arab emirates": "AE", "阿联酋": "AE",
}
LEVELS = {
    "高中": "high_school", "中学": "high_school", "美高": "high_school", "secondary": "high_school", "high_school": "high_school",
    "本科": "bachelor", "学士": "bachelor", "undergraduate": "bachelor", "bachelor": "bachelor",
    "硕士": "taught_master", "master": "taught_master", "研究生": "taught_master", "taught_master": "taught_master",
    "博士": "doctorate", "phd": "doctorate", "doctorate": "doctorate",
    "预科": "foundation", "foundation": "foundation", "文凭": "diploma", "diploma": "diploma",
    "专升本": "top_up", "top_up": "top_up", "transfer": "transfer", "转学": "transfer",
}
SYNONYMS = (
    ("community college", "社区大学"), ("community colleges", "社区大学"),
    ("美国中学", "美国高中"), ("美高", "美国高中"), ("留美高中", "美国高中"),
    ("中外合作", "合作办学"), ("合作办学办学", "合作办学"),
    ("转学分", "转学"), ("学费生活费", "预算"), ("费用预算", "预算"),
    ("职业目标", "就业目标"), ("职业规划", "就业规划"),
    ("寄宿", "住宿"), ("住家", "住宿"), ("自理能力", "生活能力"),
    ("没方向", "留学定位"), ("没有方向", "留学定位"), ("想留学", "留学定位"),
    ("不知道去哪", "留学定位"), ("不知道去哪里", "留学定位"),
)
STOP_TERMS = set("学生 学校 大学 留学 申请 专业 情况 问题 这个 那个 这些 那些 现在 目前 可以 是否 怎么 如何 什么 哪些 需要 建议 适合 可能 一些 一般 比较 知道 信息 要求 选择 学习 准备 考虑 进行 相关 关于 以及 帮我 请问 我想 想要 能不能 不能 能不 能否 有没有 不足 的话 时候 还是 自己 一个 方面 结果 内容 方法".split())
STOP_EN = set("the and for with can could should would want study abroad a an of in to is are i my me on at how what which please".split())
GENERAL_FACT_TAGS = {"学费", "费用", "预算", "总费用", "生活费", "申请", "要求", "专业", "新专业", "硕士", "本科", "博士", "奖学金", "学校", "大学", "入学", "申请季"}
POLICY_SCOPE_HINTS = {
    "CA": ("PGWP", "IRCC", "PAL", "TAL", "CAQ", "GIC", "Visitor Record"),
    "US": ("CPT", "OPT", "SEVIS", "F-1", "I-20", "DS-160", "TAG"),
    "UK": ("CAS", "Graduate visa"),
    "AU": ("CoE", "ANU", "澳国立"),
    "IE": ("Stamp 1G", "Stamp 4", "CSEP"),
    "HK": ("高才", "高才通", "IANG", "TTPS"),
    "NZ": ("Green List", "Teaching Council"),
}
# Deliberately small and extendable institution registry. Short "UCD" alone is
# ambiguous; a full name, location or official host is needed to resolve it.
SCHOOL_ENTITIES = {
    "university-college-dublin": {
        "aliases": ("University College Dublin", "都柏林大学"),
        "country": "IE", "hosts": ("ucd.ie",), "ambiguous_aliases": ("ucd",),
    },
    "university-california-davis": {
        "aliases": ("UC Davis", "U.C. Davis", "University of California Davis", "加州大学戴维斯", "加州大学戴维斯分校", "加州大学戴维斯校区"),
        "country": "US", "hosts": ("ucdavis.edu",), "ambiguous_aliases": ("ucd",),
    },
    "university-western-australia": {
        "aliases": ("University of Western Australia", "西澳大学"),
        "country": "AU", "hosts": ("uwa.edu.au",), "ambiguous_aliases": ("uwa",),
    },
    "university-west-alabama": {
        "aliases": ("University of West Alabama", "西阿拉巴马大学"),
        "country": "US", "hosts": ("uwa.edu",), "ambiguous_aliases": ("uwa",),
    },
    "university-san-francisco": {
        "aliases": ("University of San Francisco", "旧金山大学"),
        "country": "US", "hosts": ("usfca.edu",), "ambiguous_aliases": ("usf",),
    },
    "university-south-florida": {
        "aliases": ("University of South Florida", "南佛罗里达大学"),
        "country": "US", "hosts": ("usf.edu",), "ambiguous_aliases": ("usf",),
    },
    "university-canberra": {
        "aliases": ("University of Canberra", "堪培拉大学"),
        "country": "AU", "hosts": ("canberra.edu.au",), "ambiguous_aliases": ("uc",),
    },
    "university-canterbury": {
        "aliases": ("University of Canterbury", "坎特伯雷大学"),
        "country": "NZ", "hosts": ("canterbury.ac.nz",), "ambiguous_aliases": ("uc",),
    },
    "university-cincinnati": {
        "aliases": ("University of Cincinnati", "辛辛那提大学"),
        "country": "US", "hosts": ("uc.edu",), "ambiguous_aliases": ("uc",),
    },
    "university-california-system": {
        "aliases": ("University of California", "加州大学"),
        "country": "US", "hosts": ("universityofcalifornia.edu",), "ambiguous_aliases": ("uc",),
    },
    "fairleigh-dickinson-vancouver": {
        "aliases": ("Fairleigh Dickinson Vancouver", "Fairleigh Dickinson University Vancouver", "FDU Vancouver", "菲尔莱狄更斯大学温哥华", "菲尔莱狄更斯大学加拿大"),
        "country": "CA", "hosts": (), "ambiguous_aliases": ("fdu", "fairleigh dickinson university", "菲尔莱狄更斯大学"),
    },
    "fairleigh-dickinson-us": {
        "aliases": ("Fairleigh Dickinson New Jersey", "FDU New Jersey", "菲尔莱狄更斯大学美国"),
        "country": "US", "hosts": (), "ambiguous_aliases": ("fdu", "fairleigh dickinson university", "菲尔莱狄更斯大学"),
    },
}


def _today() -> dt.date:
    # Only the current Singapore date is needed, not historical timezone rules.
    # A fixed UTC+8 offset also works on Windows without an IANA/tzdata package.
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()


def _date(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("invalid_date")
    return dt.date.fromisoformat(value)


def _norm(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    for old, new in SYNONYMS:
        value = value.replace(old, new)
    return " ".join(re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", value))


def _terms(value: str) -> set[str]:
    output = {re.sub(r"\s+", "", x) for x in re.findall(r"(?<![0-9])[1-5]\s*\+\s*[1-5](?![0-9])", value)}
    for word in re.findall(r"[a-z][a-z0-9]*|[\u3400-\u9fff]+", _norm(value)):
        if word[0].isascii():
            if len(word) >= 2 and word not in STOP_EN:
                output.add(word)
        else:
            for n in (2, 3):
                # Filtering only an exact bigram leaves filler trigrams such as
                # “有什么” and “是否适” as false cross-topic evidence.
                output.update(word[i:i+n] for i in range(len(word)-n+1)
                              if not any(stop in word[i:i+n] for stop in STOP_TERMS))
    return output


def _expand_query(query: str) -> str:
    """Translate everyday funding-gap language into retrieval terms, not facts."""
    extra = []
    if re.search(r"钱|资金|学费|预算|费用", query) and re.search(r"只够|不够|不足|没(?:有)?着落|没(?:有)?落实|凑不齐|承担不了|付不起", query):
        extra.extend(("预算", "资金缺口"))
    if re.search(r"[1-5]\s*\+\s*[1-5]", query) and "国内" in query and re.search(r"国外|海外|出国", query):
        # Route a domestic/overseas study-path question to comparison methods;
        # this does not certify any specific programme as approved joint study.
        extra.append("国际本科")
    if re.search(r"先修|数学|编程",query) and re.search(r"没修|没学|没有|缺|补",query):
        extra.extend(("先修课程", "补修", "跨专业"))
    if "实习" in query and re.search(r"工资|发钱|发薪|报酬|带薪|无薪",query):
        extra.extend(("带薪实习", "企业工资", "补助"))
    if re.search(r"学位|毕业|读完|项目",query) and re.search(r"注册|执业|治疗师",query):
        extra.extend(("职业资格", "职业注册", "专业认证", "首次执业"))
    if re.search(r"网上|在线|网课|线上",query) and re.search(r"工签|工作签|居留",query):
        extra.extend(("混合授课", "实际境外学习时间"))
    if re.search(r"读书|留学",query) and "移民" in query:
        extra.extend(("留学目标", "毕业工作", "居留资格"))
    return query + (" " + " ".join(extra) if extra else "")


def _mentions(text: str, alias: str) -> bool:
    value, term = _norm(text), _norm(alias)
    if term.isascii():
        return bool(re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", value))
    return term in value


def _school_entities(text: str, urls=(), country=""):
    found = set()
    for key, entity in SCHOOL_ENTITIES.items():
        if any(_mentions(text, alias) for alias in entity["aliases"]):
            found.add(key)
        for url in urls:
            host = (urlsplit(url).hostname or "").casefold()
            if any(host == h or host.endswith("." + h) for h in entity["hosts"]):
                found.add(key)
    # A country only resolves an abbreviation when exactly one registered
    # entity in that country uses it (USF + US still names two universities).
    aliases = {a for e in SCHOOL_ENTITIES.values() for a in e["ambiguous_aliases"]}
    for alias in aliases:
        if not _mentions(text, alias):
            continue
        candidates = {k for k, e in SCHOOL_ENTITIES.items()
                      if alias in e["ambiguous_aliases"] and e["country"] == country}
        if len(candidates) == 1:
            found.update(candidates)
    return found


def _ambiguous_school_aliases(query: str, resolved):
    aliases = {a for e in SCHOOL_ENTITIES.values() for a in e["ambiguous_aliases"]}
    unresolved = []
    for alias in aliases:
        if not _mentions(query, alias):
            continue
        # UC in "UC Davis" is part of an explicit resolved name, rather than
        # a second unresolved institution mentioned alongside it.
        if any(_norm(full) != _norm(alias) and _mentions(full, alias) and _mentions(query, full)
               for key in resolved for full in SCHOOL_ENTITIES[key]["aliases"]):
            continue
        candidates = {k for k, e in SCHOOL_ENTITIES.items() if alias in e["ambiguous_aliases"]}
        if len(resolved & candidates) != 1:
            unresolved.append(alias.upper() if alias.isascii() else alias)
    return sorted(unresolved)


def _query_scope(query: str):
    """Use only unambiguous explicitly named scopes, never infer student eligibility."""
    nq = _norm(query)
    countries = set()
    for alias, country in COUNTRIES.items():
        if (not alias.isascii() and alias in nq) or (alias.isascii() and len(alias) >= 4 and re.search(r"\b" + re.escape(alias) + r"\b", nq)):
            countries.add(country)
    level_words = {
        "high_school": ("高中", "中学"), "taught_master": ("硕士", "二硕", "master"),
        "doctorate": ("博士", "phd"), "bachelor": ("本科", "专升本", "升本"),
    }
    levels = {level for level, words in level_words.items() if any(word in nq for word in words)}
    # Several countries/levels may be a comparison: keep the caller's wider scope.
    return (next(iter(countries)) if len(countries) == 1 else "",
            next(iter(levels)) if len(levels) == 1 else "")


def _subject_anchor(card: dict, query: str, country: str = "") -> bool:
    """A general fee question must not surface an arbitrary university's fee fact."""
    if card["verification_status"] == "official_verified" and card["topic"] != "院校专业":
        countries = {COUNTRIES.get(c.casefold(), c.upper()) for c in card["countries"]}
        explicit_country, _ = _query_scope(query)
        scoped = bool((country and country in countries) or (explicit_country and explicit_country in countries))
        named_policy = any(_mentions(query, hint) for cc in countries for hint in POLICY_SCOPE_HINTS.get(cc, ()))
        if countries and not (scoped or named_policy):
            return False
    if card["verification_status"] != "official_verified" or card["topic"] != "院校专业":
        return True
    entities=_school_entities(" ".join((card['title'],*card['tags'])),card['official_urls'])
    if entities:
        # A subject such as AI or biology does not identify a university.
        # Explicit names, ambiguous school abbreviations (reported separately),
        # and a specific course code may surface its school card.
        return any(_mentions(query,a) for key in entities for a in
                   (*SCHOOL_ENTITIES[key]['aliases'],*SCHOOL_ENTITIES[key]['ambiguous_aliases'])) or any(
                       re.fullmatch(r'[A-Za-z]{1,4}[0-9]{2,6}',tag) and _mentions(query,tag) for tag in card['tags'])
    nq = _norm(query)
    return any(_norm(tag) in nq for tag in card["tags"]
               if len(_norm(tag)) >= 2 and _norm(tag) not in GENERAL_FACT_TAGS
               and not _norm(tag).isdigit())


def _safe_url(value: str) -> bool:
    try:
        parts = urlsplit(value)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            return False
        host = parts.hostname.lower()
        if host == "localhost" or host.endswith((".local", ".internal")):
            return False
        try:
            if not ipaddress.ip_address(host).is_global:
                return False
        except ValueError:
            if "." not in host:
                return False
        return not any(re.search(r"token|password|secret|api.?key|authorization", key, re.I) for key, _ in parse_qsl(parts.query))
    except (ValueError, TypeError):
        return False


def _public(card: dict, today: dt.date):
    """Fail closed for publication, role, types, evidence state and current validity."""
    if card.get("publication_status") != "published" or card.get("audience") != "public":
        return None
    if not ID_RE.fullmatch(str(card.get("id", ""))):
        return None
    status, kind = card.get("verification_status"), card.get("kind")
    if status == "experience_reviewed":
        if kind not in {"method", "knowledge"}:
            return None
    elif status == "official_verified":
        if kind not in {"knowledge", "official_fact", "policy"}:
            return None
    else:
        return None
    if card.get("conflict") or card.get("superseded_by") or card.get("is_current") is False:
        return None
    out = {}
    try:
        for key in PUBLIC_FIELDS:
            value = card.get(key)
            if key in DATE_FIELDS:
                parsed = _date(value)
                out[key] = parsed.isoformat() if parsed else None
            elif key in LIST_FIELDS:
                if value is None:
                    value = []
                if not isinstance(value, list) or len(value) > 40 or any(
                    not isinstance(x, str) or len(x) > 1800 or (key != "official_urls" and PRIVATE_TEXT_RE.search(x))
                    for x in value
                ):
                    return None
                out[key] = value[:]
            else:
                if value is None:
                    value = ""
                if not isinstance(value, str) or len(value) > 12000 or PRIVATE_TEXT_RE.search(value):
                    return None
                out[key] = value
        if not all(out[key].strip() for key in ("title", "summary", "content", "source_label")):
            return None
        if any(not _safe_url(url) for url in out["official_urls"]):
            return None
        dates = {k: _date(out[k]) for k in DATE_FIELDS}
        if dates["last_verified_at"] and dates["last_verified_at"] > today:
            return None
        if dates["as_of"] and dates["as_of"] > today:
            return None
        if dates["valid_until"] and dates["valid_until"] < today:
            return None
        if dates["effective_from"] and dates["effective_from"] > today:
            return None
        if dates["review_due_at"] and dates["review_due_at"] <= today:
            return None
        if status == "official_verified" and (not dates["last_verified_at"] or not out["official_urls"]):
            return None
    except (TypeError, ValueError):
        return None
    return out


class KnowledgeStore:
    def __init__(self, path=DEFAULT_DATA, today=None):
        self.path = Path(path)
        self.today = today or _today()
        self.snapshot_metadata = {}

    def _load(self):
        self.snapshot_metadata = {}
        try:
            if self.path.stat().st_size > MAX_DATA_BYTES:
                return None, "snapshot_invalid"
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            cards = payload.get("cards") if isinstance(payload, dict) else payload
            if not isinstance(cards, list) or len(cards) > MAX_CARDS:
                return None, "snapshot_invalid"
            ids = [c.get("id") for c in cards if isinstance(c, dict)]
            if any(not isinstance(i, str) for i in ids) or len(ids) != len(set(ids)):
                return None, "snapshot_invalid"
            if isinstance(payload, dict):
                version, built = payload.get("snapshot_version"), payload.get("built_at")
                if isinstance(version, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,80}", version):
                    self.snapshot_metadata["snapshot_version"] = version
                if built and _date(built):
                    self.snapshot_metadata["snapshot_date"] = built
            published = []
            for card in cards:
                if not isinstance(card, dict):
                    continue
                public = _public(card, self.today)
                if public:
                    aliases = card.get("question_aliases", [])
                    if not isinstance(aliases, list) or any(not isinstance(a, str) or len(a) > MAX_QUERY for a in aliases):
                        aliases = []
                    published.append((public, aliases[:40]))
            return published, None
        except FileNotFoundError:
            return None, "snapshot_not_ready"
        except (OSError, UnicodeError, ValueError):
            return None, "snapshot_invalid"

    def _response(self, **kwargs):
        return {**kwargs, "meta": {"source": "bundled_study_abroad_knowledge", "as_of": self.today.isoformat(),
                **self.snapshot_metadata,
                "scope": "随包已审核知识快照；经验方法不是学校官方要求，核验范围与日期以每条为准。"}}

    def get(self, card_id: str):
        if not isinstance(card_id, str) or not ID_RE.fullmatch(card_id):
            return self._response(error="not_found", card=None)
        cards, error = self._load()
        if error:
            return self._response(error=error, card=None)
        for card, _ in cards:
            if card["id"] == card_id:
                return self._response(card=card)
        return self._response(error="not_found", card=None)

    def search(self, query: str, country: str = "", level: str = "", topic: str = "", limit: int = 5):
        if not isinstance(query, str) or not 2 <= len(query.strip()) <= MAX_QUERY:
            return self._response(error="invalid_query", results=[], count=0)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
            return self._response(error="invalid_limit", results=[], count=0)
        if any(not isinstance(v, str) or len(v) > 80 for v in (country, level, topic)):
            return self._response(error="invalid_filter", results=[], count=0)
        cc, lv = country.strip().casefold(), level.strip().casefold()
        if cc and cc not in COUNTRIES or lv and lv not in LEVELS:
            return self._response(error="invalid_filter", results=[], count=0)
        cc, lv = COUNTRIES.get(cc, ""), LEVELS.get(lv, "")
        inferred_cc, inferred_lv = _query_scope(query)
        cc, lv = cc or inferred_cc, lv or inferred_lv
        school_scope = _school_entities(query, country=cc)
        ambiguous_aliases = _ambiguous_school_aliases(query, school_scope)
        specific_school_fact = bool(school_scope and re.search(r"学费|收费|申请费|语言要求|录取要求|入学要求|申请截止|截止日期|课程代码|专业代码", query)
                                   and not re.search(r"怎么办|预算|负担|只够|不够|怎么选|适合|值不值", query))
        retrieval_query = _expand_query(query)
        if school_scope:
            retrieval_query += " " + " ".join(alias for school in sorted(school_scope) for alias in SCHOOL_ENTITIES[school]["aliases"])
        query_terms = _terms(retrieval_query)
        if not query_terms:
            return self._response(error="query_too_broad", results=[], count=0)
        cards, error = self._load()
        if error:
            return self._response(error=error, results=[], count=0)
        if ambiguous_aliases:
            candidates=[]
            for key,entity in SCHOOL_ENTITIES.items():
                if cc and entity['country'] != cc:
                    continue
                if any(a.casefold() in {x.casefold() for x in ambiguous_aliases} for a in entity['ambiguous_aliases']):
                    candidates.append({'institution':entity['aliases'][0],'country':entity['country']})
            response=self._response(error='ambiguous_institution',results=[],count=0,
                institution_candidates=candidates,
                clarification='先确认完整校名、国家或具体项目链接；确认前不报某校学费、截止日或适配结论，也不用“如果是某校”预先报价。')
            response['meta'].update(ambiguous_school_aliases=ambiguous_aliases,needs_institution_clarification=True)
            return response
        docs = []
        for card, aliases in cards:
            countries = {COUNTRIES.get(x.casefold(), x.upper()) for x in card["countries"]}
            levels = {LEVELS.get(x.casefold(), x) for x in card["levels"]}
            if cc and countries and cc not in countries:
                continue
            if lv and levels and lv not in levels:
                continue
            if topic and _norm(topic) != _norm(card["topic"]):
                continue
            card_schools = _school_entities(" ".join((card["title"], *card["tags"])), card["official_urls"])
            if school_scope and card_schools and not school_scope.intersection(card_schools):
                continue
            if specific_school_fact and (card["verification_status"] != "official_verified" or not school_scope.intersection(card_schools)):
                continue
            if not _subject_anchor(card, query, cc) and not school_scope.intersection(card_schools):
                continue
            anchors = [card["title"], card["topic"], *card["tags"], *aliases]
            anchor_terms = _terms(" ".join(anchors))
            summary_terms = _terms(card["summary"])
            body_terms = _terms(card["content"])
            docs.append((card, anchors, anchor_terms, summary_terms, body_terms))
        frequency = Counter(t for _, _, a, s, _ in docs for t in a | s)
        matches = []
        nq = _norm(retrieval_query)
        for card, anchors, anchor_terms, summary_terms, body_terms in docs:
            hits = query_terms & anchor_terms
            # Two-character generic overlap alone must not pull a random school card.
            strong = {t for t in hits if len(t) >= 3}
            exact = {_norm(a) for a in anchors if len(_norm(a)) >= 2 and _norm(a) not in STOP_TERMS
                     and (_norm(a) in nq or (len(nq) >= 3 and nq in _norm(a)))}
            exact.update(t for t in hits if re.fullmatch(r"[1-5]\+[1-5]", t))
            if not (strong or exact or len(hits) >= 2):
                continue
            def weighted(terms, weight):
                return sum(weight * (1 + math.log((len(docs)+1)/(frequency[t]+1))) for t in query_terms & terms)
            score = weighted(anchor_terms, 4) + weighted(summary_terms, 1.5) + weighted(body_terms, 0.25)
            # Exact topic/tag phrases outweigh several weak cross-field bigrams.
            # Repeated alias/tag wording counts once.
            score += 15 * min(2, len(exact))
            score /= math.sqrt(max(8, len(anchor_terms)))
            if score < 1.0:
                continue
            matches.append((score, card))
        matches.sort(key=lambda x: (-x[0], x[1]["id"]))
        results = []
        for score, card in matches[:limit]:
            brief = {k: card[k] for k in ("id", "title", "kind", "topic", "summary", "applies_when",
                     "not_applicable_when", "source_label", "verification_status", "as_of",
                     "last_verified_at", "valid_until", "review_due_at", "official_urls")}
            results.append(brief)
        response = self._response(results=results, count=len(results))
        if ambiguous_aliases:
            response["meta"]["ambiguous_school_aliases"] = ambiguous_aliases
            response["meta"]["scope_note"] = "院校简称可能指不同学校；请先结合全称或国家确认，不能仅凭简称认定院校。"
        return response


def search(query: str, country: str = "", level: str = "", topic: str = "", limit: int = 5):
    return KnowledgeStore().search(query, country, level, topic, limit)


def get(card_id: str):
    return KnowledgeStore().get(card_id)


def main(argv=None):
    parser = argparse.ArgumentParser(description="查询随包已审核知识快照（只读，不是远程学校 MCP）")
    sub = parser.add_subparsers(dest="command", required=True)
    find = sub.add_parser("search")
    find.add_argument("--query", required=True)
    find.add_argument("--country", default="")
    find.add_argument("--level", default="")
    find.add_argument("--topic", default="")
    find.add_argument("--limit", default=5, type=int)
    detail = sub.add_parser("get")
    detail.add_argument("--id", required=True, dest="card_id")
    args = vars(parser.parse_args(argv))
    command = args.pop("command")
    result = search(**args) if command == "search" else get(**args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get("error") not in (None, "not_found", "query_too_broad") else 0


if __name__ == "__main__":
    sys.exit(main())
