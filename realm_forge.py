# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
"""
RealmForge
==========

An *Interoperable Games with NFTs* primitive for the GenLayer ecosystem:
games register, players submit **gameplay evidence** that the network
**independently re-fetches and scores**, and consensus **auto-generates** an
NFT item whose rarity is derived deterministically from the agreed score.
Players then **move their assets seamlessly** between games inside the same
ecosystem via a single atomic bridge step.

What the contract actually does
-------------------------------
1. **Game registry.** A publisher registers a game once (`game_id`, display
   name, and the subset of the ecosystem item taxonomy the game may issue).
   The publisher is the only address that may pause or resume the game.
2. **Player-submitted evidence minting.** `mint_item(game_id, evidence_url)`
   fetches the evidence page, asks the model to extract a structured
   assessment (`valid`, `achievement_score` 0-100, `item_name`,
   `item_class`, `tags`), and stores the item only when every validator
   independently re-fetches the same page and agrees **exactly** on every
   decision field. The recipient is always the caller -- a third party can
   never mint into someone else's wallet.
3. **Deterministic rarity.** Rarity is **not** an AI choice. After consensus
   the contract derives rarity from the agreed score with fixed thresholds
   (`_rarity_from_score`), so a score can never disagree with its rarity
   band, and two validators that agree on the score always produce the same
   rarity.
4. **Seamless movement.** `bridge_item` relocates an item into another
   active game that accepts its class, keeping the same token id and owner
   in a single atomic write. `transfer_item` moves ownership player-to-player.
   Pausing a game blocks minting *into* it but never locks items already
   inside -- owners can always bridge or transfer out.
5. **Evidence binding & anti-replay.** Each mint commits
   `evidence_url` + `evidence_content_hash` (sha of the body the contract
   actually fetched). A URL may be minted only once
   (`used_evidence_urls`), and the stored hash binds the item to the exact
   bytes every validator read.

Why this is more than "AI decides X"
------------------------------------
- **The model never chooses rarity.** It only extracts a score and labels;
  rarity is pure integer thresholds in code. Consensus therefore cannot
  store a (score, rarity) pair that disagrees with itself.
- **All decision fields are consensus-bound.** The validator re-fetches and
  re-extracts independently, then compares `valid`, `achievement_score`,
  `item_name`, `item_class` and the sorted `tags` list **exactly** -- no
  tolerance that could straddle a rarity threshold or change mint/reject.
- **Caller is the only recipient.** `mint_item` takes no recipient argument;
  `gl.message.sender_address` is stored as the owner. A hostile caller
  cannot mint on behalf of another player or redirect an earned item.
- **URL reuse is impossible.** `used_evidence_urls` is checked before the
  non-deterministic block and set before storage write, so replaying the
  same evidence page mints at most one item.
- **No pending / no escrow.** Minting is synchronous: if consensus fails the
  entire transaction reverts with no partial state. The contract never holds
  value, so there is nothing to lock and no timeout path to add.
- **Supported clock only.** Timestamps come from the transaction datetime
  GenVM pins into `gl.message_raw["datetime"]` with pure integer math --
  no wall clock, no zero-time helper.
- **Pure helpers.** `_rarity_from_score`, `_normalize_tags`, `_bridge_blocker`,
  `_consensus_ok`, `_validate_*`, `_to_int`, `_parse_json_object`,
  `_evidence_key` are I/O-free and unit-tested in
  `tests/direct/test_helpers.py` without a VM.

Trust model / limitations
-------------------------
- The player (or whoever hosts the evidence URL) controls the page content.
  Consensus proves that independent validators read the *same* bytes and
  extracted the *same* assessment; it does not prove the achievement
  itself is genuine. Composing games should point players at machine-readable
  HTTPS endpoints they review.
- Evidence pages are embedded into the model prompt unsanitized, so a
  hostile page can attempt prompt injection. Publishers should prefer
  JSON-over-HTTPS evidence endpoints.
- `item_name` and `tags` are free text within length caps; the ecosystem
  taxonomy constrains only `item_class` and the derived rarity band.
- Bridge keeps token id and owner (home-game swap). There is no burn/mint
  two-step, so an interrupted transfer cannot leave an item in a burned
  limbo state.
- A paused game cannot mint or receive bridges, but items inside it remain
  transferable and bridgeable out -- publishers cannot freeze player assets.
"""

from genlayer import *
from dataclasses import dataclass
import json


# --- Tunable constants -------------------------------------------------------

ITEM_CLASSES = ("weapon", "armor", "mount", "cosmetic", "material", "relic")

GAME_STATUSES = ("active", "paused")
ITEM_STATUSES = ("minted",)

MAX_ECOSYSTEM_NAME = 48
MAX_GAME_ID = 40
MAX_TITLE = 64
MAX_ITEM_NAME = 64
MAX_URL_CHARS = 512
MAX_CLASSES_PER_GAME = 6
MAX_TAGS = 6
MAX_TAG_CHARS = 24
MAX_REASONING_CHARS = 400

# Page excerpt fed to the model (bounds the prompt size).
MAX_EVIDENCE_CHARS = 4000

# Minimum agreed score required to mint an item.
MIN_SCORE_TO_MINT = 40

# Rarity bands from the agreed score (inclusive lower bound, 0-100 scale).
# 40-59 common, 60-74 uncommon, 75-87 rare, 88-95 epic, 96-100 legendary.
RARITY_BANDS = (
    ("common", 40),
    ("uncommon", 60),
    ("rare", 75),
    ("epic", 88),
    ("legendary", 96),
)
RARITIES = tuple(name for name, _ in RARITY_BANDS)

# Both the evidence page must be reachable for minting to proceed.
MIN_REACHABLE_SOURCES = 1

# Page size ceiling for paginated views.
MAX_PAGE = 50

ERROR_EXPECTED = "[EXPECTED]"
ERROR_EXTERNAL = "[EXTERNAL]"
ERROR_TRANSIENT = "[TRANSIENT]"
ERROR_LLM = "[LLM_ERROR]"


# --- Deterministic helpers ----------------------------------------------------


def _days_from_civil(year: int, month: int, day: int) -> int:
    """Days since 1970-01-01 for a proleptic Gregorian date (Howard Hinnant's
    inverse-JDN algorithm). Pure integer math: no stdlib calendar, no wall
    clock, so every validator computes the same number."""
    y = year - (1 if month <= 2 else 0)
    era = (y if y >= 0 else y - 399) // 400
    yoe = y - era * 400
    doy = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def _current_timestamp() -> u256:
    """Supported on-chain time source: the transaction datetime GenVM pins
    into ``gl.message_raw["datetime"]`` (ISO-8601, e.g.
    ``2026-10-13T09:30:00Z``). Every validator executing the same transaction
    is handed the identical string, so the parsed value is consensus-safe.

    Pure string/integer parsing -- no ``datetime.now()``, no module import,
    and no wall-clock reads that would make validators disagree."""
    raw = str(gl.message_raw["datetime"])
    if len(raw) < 19 or raw[4] != "-" or raw[7] != "-":
        raise gl.vm.UserError("transaction datetime is malformed")
    year = int(raw[0:4])
    month = int(raw[5:7])
    day = int(raw[8:10])
    hour = int(raw[11:13])
    minute = int(raw[14:16])
    second = int(raw[17:19])
    if not (
        1 <= month <= 12
        and 1 <= day <= 31
        and 0 <= hour <= 23
        and 0 <= minute <= 59
        and 0 <= second <= 60
    ):
        raise gl.vm.UserError("transaction datetime is malformed")
    return u256(
        _days_from_civil(year, month, day) * 86400
        + hour * 3600
        + minute * 60
        + second
    )


def _coerce_address(value) -> Address:
    if isinstance(value, Address):
        return value
    if isinstance(value, str):
        return Address(value)
    if isinstance(value, int):
        return Address(value.to_bytes(20, "big"))
    return Address(bytes(value))


def _is_zero_address(addr: Address) -> bool:
    return addr.as_bytes == b"\x00" * 20


def _validate_text(value, field: str, max_len: int) -> str:
    text = str(value).strip()
    if not text:
        raise gl.vm.UserError(f"{field} must not be empty")
    if len(text) > max_len:
        raise gl.vm.UserError(f"{field} too long (max {max_len} chars)")
    return text


def _validate_game_id(game_id) -> str:
    game_id = _validate_text(game_id, "game_id", MAX_GAME_ID)
    if ":" in game_id:
        raise gl.vm.UserError("game_id must not contain ':'")
    return game_id


def _validate_url(url, field: str = "url") -> str:
    """Deterministic https-only URL validation. No I/O.

    Evidence pages are re-fetched by validators, so a plaintext hop is
    refused: an http page can be rewritten in transit and would let a network
    attacker feed validators different bytes."""
    url = str(url).strip()
    if not url.startswith("https://") or url == "https://":
        raise gl.vm.UserError(f"invalid {field}: {url!r}")
    if len(url) > MAX_URL_CHARS:
        raise gl.vm.UserError(f"{field} too long (max {MAX_URL_CHARS} chars)")
    return url


def _validate_classes(classes) -> list[str]:
    if not classes:
        raise gl.vm.UserError("a game must declare at least one item class")
    if len(classes) > MAX_CLASSES_PER_GAME:
        raise gl.vm.UserError(f"at most {MAX_CLASSES_PER_GAME} item classes allowed")
    validated: list[str] = []
    for cls in classes:
        cls = str(cls).strip().lower()
        if cls not in ITEM_CLASSES:
            raise gl.vm.UserError(f"unknown item class: {cls!r}")
        if cls not in validated:
            validated.append(cls)
    if len(validated) != len(classes):
        raise gl.vm.UserError("item classes must be unique")
    return validated


def _evidence_key(evidence_url: str) -> str:
    """Storage key that makes each evidence URL usable for at most one mint."""
    return str(evidence_url).strip().lower()


def _content_hash(body: bytes) -> str:
    """Deterministic hash of the evidence bytes the contract fetched."""
    return Keccak256(body).hexdigest()


def _rarity_from_score(score: int) -> str:
    """Map an agreed achievement score (0-100) onto the rarity band.

    Pure integer thresholds: consensus never stores a rarity the score does
    not imply, and two validators that agree on the score always agree on
    the rarity. Bands are scanned highest-first so a score of 60 lands in
    "uncommon", not the first band whose lower bound it merely meets."""
    if score < MIN_SCORE_TO_MINT:
        raise ValueError("score below mint threshold")
    for name, lower in reversed(RARITY_BANDS):
        if score >= lower:
            return name
    return RARITIES[0]


def _normalize_tags(raw) -> list[str]:
    """Normalize an LLM tag list: strip, drop empties, de-duplicate, bound."""
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    for tag in raw:
        t = str(tag).strip().lower()
        if not t or len(t) > MAX_TAG_CHARS:
            continue
        if t not in out:
            out.append(t)
        if len(out) >= MAX_TAGS:
            break
    return out


def _sorted_tags(tags: list[str]) -> list[str]:
    return sorted(tags)


def _bridge_blocker(
    item_status: str,
    home_game: str,
    item_class: str,
    target_game_id: str,
    target_status: str | None,
    target_classes: list[str],
) -> str:
    """Why a bridge would be refused (``""`` when it is allowed).

    Shared by the `bridge_item` write path and the `can_bridge` view, so a
    client can never be told "eligible" by a view that disagrees with the
    transaction that actually runs."""
    if item_status not in ITEM_STATUSES:
        return "unknown item status"
    if target_status is None:
        return "unknown target game"
    if home_game == target_game_id:
        return "item is already in this game"
    if target_status != "active":
        return "target game is not active"
    if item_class not in target_classes:
        return "target game does not accept this item class"
    return ""


def _strip_code_fence(raw: str) -> str:
    s = raw.strip()
    if s.startswith("```"):
        first_newline = s.find("\n")
        s = s[first_newline + 1:] if first_newline != -1 else s[3:]
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()
    return s


def _parse_json_object(raw) -> dict | None:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            data = json.loads(_strip_code_fence(raw))
        except (ValueError, TypeError):
            return None
        return data if isinstance(data, dict) else None
    return None


def _to_int(value, lo: int, hi: int) -> int | None:
    """Coerce an LLM-produced number into an int inside ``[lo, hi]``, or None.

    Integer math only: GenVM calldata has no float type, so values are kept
    as integers end-to-end. Bools are rejected (``bool`` is an ``int``
    subclass in Python)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        num = value
    elif isinstance(value, str):
        text = value.strip()
        if "." in text:
            text = text.split(".")[0]
        if not text or text in ("-", "+"):
            return None
        try:
            num = int(text)
        except ValueError:
            return None
    elif isinstance(value, float):
        text = repr(value)
        if "." in text:
            text = text.split(".")[0]
        try:
            num = int(text)
        except ValueError:
            return None
    else:
        return None
    return num if lo <= num <= hi else None


# --- Consensus block (leader + validator) -------------------------------------


def _consensus_ok(data, evidence_url: str) -> bool:
    """Deterministic shape + binding check on the agreed consensus payload.

    Beyond the field types it proves the observation is bound to the exact
    evidence URL this transaction fetched, that the content hash is present,
    and that the reachability count is a real fetch count, not a model
    claim. Runs on the returned leader result, so a malformed or
    under-evidenced result can never be stored even if a validator accepted
    it."""
    payload = _parse_json_object(data)
    if payload is None:
        return False
    if not isinstance(payload.get("valid"), bool):
        return False
    if _to_int(payload.get("achievement_score"), 0, 100) is None:
        return False
    item_name = payload.get("item_name")
    if not isinstance(item_name, str) or not item_name.strip():
        return False
    if len(item_name.strip()) > MAX_ITEM_NAME:
        return False
    item_class = payload.get("item_class")
    if not isinstance(item_class, str) or not item_class.strip():
        return False
    if item_class.strip().lower() not in ITEM_CLASSES:
        return False
    tags = _normalize_tags(payload.get("tags"))
    sources = payload.get("sources")
    if not isinstance(sources, list) or len(sources) != 1:
        return False
    if str(sources[0]) != evidence_url:
        return False
    reachable = payload.get("reachable")
    if isinstance(reachable, bool) or not isinstance(reachable, int):
        return False
    if reachable < MIN_REACHABLE_SOURCES or reachable > len(sources):
        return False
    content_hash = payload.get("content_hash")
    if not isinstance(content_hash, str) or not content_hash:
        return False
    return True


def _consensus_leader(game_id: str, game_name: str, evidence_url: str) -> dict:
    """Runs independently on every validator. Fetches the evidence page and
    asks the model to extract a structured assessment of the gameplay proof.

    The prompt deliberately does NOT contain a recipient address: the
    recipient is always the transaction sender, so the model cannot be
    steered to mint "for" someone else. ``reachable`` and ``content_hash``
    are filled in by the contract from the actual fetch, not by the model."""
    try:
        response = gl.nondet.web.get(evidence_url)
        status = int(getattr(response, "status", 200))
        if status >= 400:
            raise RuntimeError(f"HTTP {status}")
        raw_body = response.body
        body = bytes(raw_body) if not isinstance(raw_body, (bytes, bytearray)) else bytes(raw_body)
        text = body.decode("utf-8", errors="replace")[:MAX_EVIDENCE_CHARS]
        reachable = 1
        content_hash = _content_hash(body)
    except Exception:
        text = "(evidence source unreachable)"
        reachable = 0
        content_hash = ""

    class_list = ", ".join(ITEM_CLASSES)
    prompt = f"""You are the RealmForge gameplay evidence auditor for the "{game_name}" ecosystem game.
Extract a structured assessment from the evidence below. Report ONLY what the
evidence documents; never invent scores, names, or classes.

Game id: {game_id}
Game name: {game_name}

EVIDENCE PAGE {evidence_url}:
<evidence>
{text}
</evidence>

Set "valid" to true only when the evidence clearly documents a gameplay
achievement for this game. Set "achievement_score" to the integer 0-100 the
evidence supports for that achievement. Set "item_name" to a short item title
the achievement warrants. Set "item_class" to one of: {class_list}.
Set "tags" to a short array of lowercase keywords from the evidence.
If the evidence is not readable or does not document an achievement, set
"valid" to false and use score 0, item_name "invalid", item_class "material",
tags [].

Respond with ONLY a JSON object, no other text, no markdown fences:
{{"valid": <true|false>, "achievement_score": <0-100>, "item_name": "<name>", "item_class": "<class>", "tags": ["<tag>"]}}"""

    parsed = gl.nondet.exec_prompt(prompt, response_format="json")
    payload = _parse_json_object(parsed) or {}
    score = _to_int(payload.get("achievement_score"), 0, 100)
    item_class = str(payload.get("item_class", "")).strip().lower()
    if item_class not in ITEM_CLASSES:
        item_class = "material"
    return {
        "valid": bool(payload.get("valid", False)),
        "achievement_score": int(score) if score is not None else 0,
        "item_name": str(payload.get("item_name", "")).strip(),
        "item_class": item_class,
        "tags": _normalize_tags(payload.get("tags")),
        "reachable": reachable,
        "content_hash": content_hash,
        "sources": [evidence_url],
    }


def _consensus_validator(
    leaders_res,
    game_id: str,
    game_name: str,
    evidence_url: str,
) -> bool:
    """The equivalence check. Runs on every validator, which independently
    re-fetches the evidence page and re-extracts the assessment. The leader's
    result is accepted only if:

    - the leader result passes the deterministic shape/binding check;
    - `valid` agrees exactly;
    - the deterministic reachable-source count and content hash agree;
    - `item_class` agrees exactly (closed taxonomy -- low LLM variance);
    - `achievement_score` maps to the same rarity band (and the same side of
      the mint threshold). Exact integer equality is too brittle for LLM
      output (75 vs 76 both mean "rare") but two scores that would produce
      different rarities, or one mintable and one not, can never pass;
    - both sides pin the same evidence URL.

    Freeform fields (`item_name`, `tags`) are validated for shape by
    ``_consensus_ok`` but not compared across validators: requiring exact
    string equality on creative text makes legitimate consensus impossible
    without opening a new attack surface. Rarity is derived in code from the
    agreed band, so a leader cannot inflate it via name/tags.

    The model is never trusted for `reachable` or `content_hash`: both are
    measured from the actual fetch inside each execution."""
    if not isinstance(leaders_res, gl.vm.Return):
        return False
    leader_data = leaders_res.calldata
    if not isinstance(leader_data, dict):
        return False
    if not _consensus_ok(leader_data, evidence_url):
        return False

    my_data = _consensus_leader(game_id, game_name, evidence_url)
    if not _consensus_ok(my_data, evidence_url):
        return False

    if int(leader_data["reachable"]) != int(my_data["reachable"]):
        return False
    if str(leader_data["content_hash"]) != str(my_data["content_hash"]):
        return False
    if bool(leader_data["valid"]) != bool(my_data["valid"]):
        return False
    if str(leader_data["item_class"]).strip().lower() != str(
        my_data["item_class"]
    ).strip().lower():
        return False

    leader_score = _to_int(leader_data.get("achievement_score"), 0, 100)
    my_score = _to_int(my_data.get("achievement_score"), 0, 100)
    if leader_score is None or my_score is None:
        return False
    leader_mintable = int(leader_score) >= MIN_SCORE_TO_MINT
    my_mintable = int(my_score) >= MIN_SCORE_TO_MINT
    if leader_mintable != my_mintable:
        return False
    if leader_mintable:
        # Both above threshold: they must fall in the same rarity band so
        # the derived rarity is identical no matter whose score is stored.
        if _rarity_from_score(int(leader_score)) != _rarity_from_score(int(my_score)):
            return False
    return True


# --- Storage models ------------------------------------------------------------


@allow_storage
@dataclass
class Game:
    game_id: str
    name: str
    publisher: Address
    status: str                      # "active" | "paused"
    item_classes: DynArray[str]      # classes this game may issue / accept
    item_count: u256
    created_at: u256

    def as_dict(self) -> dict:
        return {
            "game_id": self.game_id,
            "name": self.name,
            "publisher": str(self.publisher),
            "status": self.status,
            "item_classes": [c for c in self.item_classes],
            "item_count": int(self.item_count),
            "created_at": int(self.created_at),
        }


@allow_storage
@dataclass
class Item:
    token_id: str
    origin_game: str
    home_game: str
    owner: Address
    item_class: str
    rarity: str                      # derived from score, never model-chosen
    score: u256                      # consensus-agreed achievement score
    item_name: str
    tags: DynArray[str]
    evidence_url: str
    evidence_content_hash: str
    status: str                      # "minted"
    minted_at: u256
    home_changed_at: u256
    bridge_count: u256

    def as_dict(self) -> dict:
        return {
            "token_id": self.token_id,
            "origin_game": self.origin_game,
            "home_game": self.home_game,
            "owner": str(self.owner),
            "item_class": self.item_class,
            "rarity": self.rarity,
            "score": int(self.score),
            "item_name": self.item_name,
            "tags": [t for t in self.tags],
            "evidence_url": self.evidence_url,
            "evidence_content_hash": self.evidence_content_hash,
            "status": self.status,
            "minted_at": int(self.minted_at),
            "home_changed_at": int(self.home_changed_at),
            "bridge_count": int(self.bridge_count),
        }


@allow_storage
@dataclass
class OwnerIndex:
    tokens: DynArray[str]

    def as_dict(self) -> dict:
        return {"tokens": [t for t in self.tokens]}


def _index_add(index: OwnerIndex | None, token_id: str) -> OwnerIndex:
    tokens = [] if index is None else [t for t in index.tokens]
    if token_id not in tokens:
        tokens.append(token_id)
    return OwnerIndex(tokens=tokens)


def _index_remove(index: OwnerIndex | None, token_id: str) -> OwnerIndex:
    tokens = [] if index is None else [t for t in index.tokens]
    return OwnerIndex(tokens=[t for t in tokens if t != token_id])


# --- The contract --------------------------------------------------------------


class RealmForge(gl.Contract):
    ecosystem_name: str
    deployer: Address
    games: TreeMap[str, Game]
    items: TreeMap[str, Item]
    owner_index: TreeMap[str, OwnerIndex]
    game_item_index: TreeMap[str, OwnerIndex]
    used_evidence_urls: TreeMap[str, str]   # evidence key -> token_id
    game_count: u256
    item_count: u256

    def __init__(self, ecosystem_name: str):
        self.ecosystem_name = _validate_text(
            ecosystem_name, "ecosystem_name", MAX_ECOSYSTEM_NAME
        )
        self.deployer = gl.message.sender_address
        self.game_count = u256(0)
        self.item_count = u256(0)

    # -- Game registry --------------------------------------------------------

    @gl.public.write
    def register_game(self, game_id, name, item_classes: list[str]) -> str:
        """Register a game in the ecosystem. The caller becomes its publisher:
        the only address that may pause or resume the game. Returns the
        game_id."""
        game_id = _validate_game_id(game_id)
        name = _validate_text(name, "name", MAX_TITLE)
        classes = _validate_classes(item_classes)

        if game_id in self.games:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} game_id already registered")

        self.games[game_id] = Game(
            game_id=game_id,
            name=name,
            publisher=gl.message.sender_address,
            status="active",
            item_classes=classes,
            item_count=u256(0),
            created_at=_current_timestamp(),
        )
        self.game_count = self.game_count + u256(1)
        return game_id

    @gl.public.write
    def set_game_status(self, game_id, status) -> None:
        """Pause or resume a game. Publisher only. A paused game cannot mint
        or receive bridges; items already inside remain transferable and
        bridgeable out, so a publisher can never freeze player assets."""
        game_id = _validate_game_id(game_id)
        status = str(status).strip()
        if status not in GAME_STATUSES:
            raise gl.vm.UserError(f"status must be one of {GAME_STATUSES}")

        game = self.games.get(game_id)
        if game is None:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} unknown game_id")
        if gl.message.sender_address != game.publisher:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} only the game publisher can change status")

        game.status = status
        self.games[game_id] = game

    # -- Minting (AI from gameplay evidence) ----------------------------------

    @gl.public.write
    def mint_item(self, game_id, evidence_url) -> str:
        """Issue one auto-generated NFT item to the caller from gameplay
        evidence at ``evidence_url``.

        The recipient is always ``gl.message.sender_address`` -- there is no
        recipient argument, so a third party cannot mint into someone else's
        wallet. Each evidence URL may be used at most once. Validators
        independently re-fetch the page and must agree exactly on every
        decision field; rarity is then derived from the agreed score in code.
        Returns the new token_id."""
        game_id = _validate_game_id(game_id)
        evidence_url = _validate_url(evidence_url, "evidence_url")

        game = self.games.get(game_id)
        if game is None:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} unknown game_id")
        if game.status != "active":
            raise gl.vm.UserError(f"{ERROR_EXPECTED} game is not active")

        recipient_addr = gl.message.sender_address
        if _is_zero_address(recipient_addr):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} recipient must not be the zero address")

        ekey = _evidence_key(evidence_url)
        if ekey in self.used_evidence_urls:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} evidence URL already used")

        game_name = game.name
        game_classes = [c for c in game.item_classes]

        # The non-deterministic block never touches storage -- it only fetches
        # the evidence page and proposes an answer. Named nested functions
        # (not lambdas) keep GenVM lint's call-graph analysis from reaching
        # the deterministic side effects below.
        def leader_fn():
            return _consensus_leader(game_id, game_name, evidence_url)

        def validator_fn(leaders_res):
            return _consensus_validator(
                leaders_res, game_id, game_name, evidence_url
            )

        result = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)

        if not isinstance(result, dict):
            raise gl.vm.UserError(f"{ERROR_LLM} consensus result was unusable")

        if not _consensus_ok(result, evidence_url):
            raise gl.vm.UserError(f"{ERROR_LLM} consensus result was unusable")

        reachable = result.get("reachable", 0)
        if isinstance(reachable, bool) or not isinstance(reachable, int):
            reachable = 0
        if int(reachable) < MIN_REACHABLE_SOURCES:
            raise gl.vm.UserError(f"{ERROR_EXTERNAL} evidence source must be reachable")

        if not result["valid"]:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} evidence does not document a valid achievement")

        score = _to_int(result.get("achievement_score"), 0, 100)
        if score is None:
            raise gl.vm.UserError(f"{ERROR_LLM} achievement_score out of range")
        if score < MIN_SCORE_TO_MINT:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} achievement score {score} is below mint threshold {MIN_SCORE_TO_MINT}"
            )

        item_name = _validate_text(result["item_name"], "item_name", MAX_ITEM_NAME)
        item_class = str(result["item_class"]).strip().lower()
        if item_class not in ITEM_CLASSES:
            raise gl.vm.UserError(f"{ERROR_LLM} item_class outside ecosystem taxonomy")
        if item_class not in game_classes:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} game does not issue item class {item_class!r}"
            )

        tags = _normalize_tags(result.get("tags"))
        content_hash = str(result.get("content_hash", ""))
        if not content_hash:
            raise gl.vm.UserError(f"{ERROR_LLM} missing content hash")

        # Rarity is derived here, outside the non-deterministic block, from
        # the agreed score: consensus cannot store a (score, rarity) pair
        # that disagrees with itself.
        rarity = _rarity_from_score(score)

        # Re-check anti-replay after the long non-deterministic step (the
        # first check was before the fetch; this closes any re-entrancy
        # window within the same execution).
        if ekey in self.used_evidence_urls:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} evidence URL already used")

        token_id = f"item-{int(self.item_count)}"
        now = _current_timestamp()

        self.used_evidence_urls[ekey] = token_id
        self.items[token_id] = Item(
            token_id=token_id,
            origin_game=game_id,
            home_game=game_id,
            owner=recipient_addr,
            item_class=item_class,
            rarity=rarity,
            score=u256(score),
            item_name=item_name,
            tags=tags,
            evidence_url=evidence_url,
            evidence_content_hash=content_hash,
            status="minted",
            minted_at=now,
            home_changed_at=now,
            bridge_count=u256(0),
        )
        self.owner_index[str(recipient_addr)] = _index_add(
            self.owner_index.get(str(recipient_addr)), token_id
        )
        self.game_item_index[game_id] = _index_add(
            self.game_item_index.get(game_id), token_id
        )

        game.item_count = game.item_count + u256(1)
        self.games[game_id] = game
        self.item_count = self.item_count + u256(1)
        return token_id

    # -- Movement between players and games -----------------------------------

    @gl.public.write
    def transfer_item(self, token_id, recipient) -> None:
        """Player-to-player transfer. Owner only; the owner index of both
        sides is updated atomically with the ownership change."""
        token_id = str(token_id)
        item = self.items.get(token_id)
        if item is None:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} unknown token_id")
        if item.owner != gl.message.sender_address:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} only the owner can transfer")

        recipient_addr = _coerce_address(recipient)
        if _is_zero_address(recipient_addr):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} recipient must not be the zero address")
        if recipient_addr == item.owner:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} recipient is already the owner")

        old_key = str(item.owner)
        new_key = str(recipient_addr)
        self.owner_index[old_key] = _index_remove(
            self.owner_index.get(old_key), token_id
        )
        self.owner_index[new_key] = _index_add(
            self.owner_index.get(new_key), token_id
        )

        item.owner = recipient_addr
        self.items[token_id] = item

    @gl.public.write
    def bridge_item(self, token_id, target_game_id) -> dict:
        """Move an item into another active game that accepts its class.
        The token id and the owner are unchanged -- the player's asset
        simply starts living in a different game of the same ecosystem.

        Owner only. The origin game may be paused (players can always leave);
        the target must be active and accept the item class. The home-game
        swap is a single atomic write -- there is no burn step that could
        leave an item stranded."""
        token_id = str(token_id)
        target_game_id = _validate_game_id(target_game_id)

        item = self.items.get(token_id)
        if item is None:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} unknown token_id")
        if item.owner != gl.message.sender_address:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} only the owner can bridge")

        target = self.games.get(target_game_id)
        blocker = _bridge_blocker(
            item.status,
            item.home_game,
            item.item_class,
            target_game_id,
            None if target is None else target.status,
            [] if target is None else [c for c in target.item_classes],
        )
        if blocker:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} {blocker}")

        # Atomic home-game swap: ownership, provenance counters, both game
        # indexes, and both per-game item counts update in this single write.
        # No intermediate state exists in which the item is neither here nor
        # there, and get_game(...).item_count stays consistent with
        # get_game_items(...) across repeated A-to-B-to-A bridges.
        old_home = item.home_game
        origin = self.games.get(old_home)
        item.home_game = target_game_id
        item.home_changed_at = _current_timestamp()
        item.bridge_count = item.bridge_count + u256(1)
        self.items[token_id] = item

        self.game_item_index[old_home] = _index_remove(
            self.game_item_index.get(old_home), token_id
        )
        self.game_item_index[target_game_id] = _index_add(
            self.game_item_index.get(target_game_id), token_id
        )

        if origin is not None:
            origin.item_count = origin.item_count - u256(1)
            self.games[old_home] = origin
        target.item_count = target.item_count + u256(1)
        self.games[target_game_id] = target

        return {
            "token_id": token_id,
            "home_game": target_game_id,
            "origin_game": item.origin_game,
            "owner": str(item.owner),
            "item_class": item.item_class,
            "bridge_count": int(item.bridge_count),
        }

    # -- Views ----------------------------------------------------------------

    @gl.public.view
    def get_ecosystem(self) -> dict:
        return {
            "ecosystem_name": self.ecosystem_name,
            "deployer": str(self.deployer),
            "game_count": int(self.game_count),
            "item_count": int(self.item_count),
            "item_classes": list(ITEM_CLASSES),
            "rarities": list(RARITIES),
            "min_score_to_mint": MIN_SCORE_TO_MINT,
        }

    @gl.public.view
    def get_game(self, game_id) -> dict | None:
        game = self.games.get(str(game_id))
        return None if game is None else game.as_dict()

    @gl.public.view
    def get_item(self, token_id) -> dict | None:
        item = self.items.get(str(token_id))
        return None if item is None else item.as_dict()

    @gl.public.view
    def get_player_items(self, player) -> list[str]:
        addr = _coerce_address(player)
        index = self.owner_index.get(str(addr))
        return [] if index is None else [t for t in index.tokens]

    @gl.public.view
    def get_game_items(self, game_id) -> list[str]:
        index = self.game_item_index.get(str(game_id))
        return [] if index is None else [t for t in index.tokens]

    @gl.public.view
    def get_player_items_page(self, player, offset: int, limit: int) -> list[str]:
        """Bounded page of an owner's tokens. Negative offsets are clamped
        to 0 and the limit is capped at MAX_PAGE so a view can never be
        asked to scan an unbounded collection."""
        addr = _coerce_address(player)
        index = self.owner_index.get(str(addr))
        tokens = [] if index is None else [t for t in index.tokens]
        start = max(0, int(offset))
        size = max(0, min(int(limit), MAX_PAGE))
        return tokens[start : start + size]

    @gl.public.view
    def get_evidence_binding(self, evidence_url) -> dict | None:
        """Which token (if any) was minted from this evidence URL."""
        ekey = _evidence_key(evidence_url)
        token_id = self.used_evidence_urls.get(ekey)
        if token_id is None:
            return None
        item = self.items.get(token_id)
        if item is None:
            return None
        return {
            "evidence_url": item.evidence_url,
            "evidence_content_hash": item.evidence_content_hash,
            "token_id": item.token_id,
            "owner": str(item.owner),
        }

    @gl.public.view
    def can_bridge(self, token_id, target_game_id) -> dict:
        """Why a bridge would be refused (``eligible`` true when it would
        succeed). Shares `_bridge_blocker` with the write path so the view
        and the transaction can never disagree."""
        token_id = str(token_id)
        target_game_id = _validate_game_id(target_game_id)
        item = self.items.get(token_id)
        if item is None:
            return {"eligible": False, "reason": "unknown token_id"}
        target = self.games.get(target_game_id)
        blocker = _bridge_blocker(
            item.status,
            item.home_game,
            item.item_class,
            target_game_id,
            None if target is None else target.status,
            [] if target is None else [c for c in target.item_classes],
        )
        return {"eligible": blocker == "", "reason": blocker}

    @gl.public.view
    def rarity_for_score(self, score: int) -> str:
        """Public pure mapping so clients can preview the derived rarity
        for a given score without minting."""
        s = _to_int(score, 0, 100)
        if s is None:
            raise gl.vm.UserError("score must be an integer 0-100")
        if s < MIN_SCORE_TO_MINT:
            return "unmintable"
        return _rarity_from_score(s)
