# RealmForge

An *Interoperable Games with NFTs* Intelligent Contract for the GenLayer
ecosystem: games register, players submit **gameplay evidence** that the
network **independently re-fetches and scores**, and consensus
**auto-generates** an NFT item whose rarity is derived deterministically
from the agreed score. Players move assets seamlessly between games with a
single atomic bridge step.

## What it does

| Capability | Method | Consensus? |
|---|---|---|
| Register a game | `register_game` | deterministic |
| Pause / resume a game (publisher only) | `set_game_status` | deterministic |
| Auto-generate an NFT from gameplay evidence | `mint_item` | **AI consensus** |
| Player-to-player transfer | `transfer_item` | deterministic |
| Bridge into another game | `bridge_item` | deterministic |
| Read ecosystem / game / item / indexes | `get_*`, `can_bridge` | view |

## Security model (audit notes)

- **Recipient is always the caller.** `mint_item` has no recipient argument;
  a third party cannot mint into someone else's wallet.
- **Evidence anti-replay.** Each evidence URL is usable at most once
  (`used_evidence_urls`); the stored `evidence_content_hash` binds the item
  to the exact bytes every validator read.
- **All decision fields are consensus-bound.** Validators re-fetch and
  re-extract independently, then compare `valid`, `achievement_score`,
  `item_name`, `item_class` and sorted `tags` **exactly**. No tolerance can
  straddle a rarity threshold.
- **Rarity is derived in code, never by the model.** After consensus the
  contract runs `_rarity_from_score`; a (score, rarity) pair that disagrees
  with itself is impossible.
- **No burn/mint limbo.** Bridging is a home-game swap in one atomic write.
  Pausing a game blocks minting *into* it but never freezes items already
  inside -- owners can always bridge or transfer out.
- **No escrow, no pending state.** The contract never holds value. Minting is
  synchronous: consensus failure reverts the entire transaction.
- **Supported clock only.** Timestamps come from `gl.message_raw["datetime"]`
  with pure integer math -- no wall clock, no zero-time helper.
- **Error prefixes.** Deterministic business errors use `[EXPECTED]`,
  external fetch failures `[EXTERNAL]`, unusable model output `[LLM_ERROR]`.

## Layout

```
realm-forge/
├── contracts/realm_forge.py
├── tests/direct/          # fast unit tests (mocked web/LLM)
├── tests/integration/     # live Studio tests (opt-in mint)
├── tools/deploy.py
├── gltest.config.yaml
├── deployment.toml
└── requirements.txt
```

## Develop

```bash
pip install -r requirements.txt

# Lint before anything else
genvm-lint check contracts/realm_forge.py

# Direct tests (no server, ~30ms each)
pytest tests/direct/ -v

# Integration (requires deployed address)
export REALM_FORGE_ADDRESS=0x...
pytest tests/integration/ -v

# Optional live AI mint during integration
export REALM_FORGE_MINT=1
export REALM_FORGE_EVIDENCE_URL=https://example.com/achievement.json
```

## Deploy

```bash
python tools/deploy.py --network localnet
python tools/deploy.py --network studionet --ecosystem "My Games"
```

## Evidence page format (recommended)

Host a JSON-over-HTTPS page that documents the achievement:

```json
{
  "player_achievement": "boss_defeated",
  "boss": "ashen_wyrm",
  "difficulty": "hard",
  "timestamp": "2026-10-01T12:00:00Z"
}
```

Validators re-fetch this URL; the model extracts a structured assessment;
rarity is then derived from the agreed score in contract code.

## Rarity bands (score -> rarity)

| Score | Rarity |
|---|---|
| 40-59 | common |
| 60-74 | uncommon |
| 75-87 | rare |
| 88-95 | epic |
| 96-100 | legendary |

Scores below 40 cannot mint.
