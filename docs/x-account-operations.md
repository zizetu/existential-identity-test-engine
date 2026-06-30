# X Account Operations — tical-code Canonical Knowledge

This document is the operating manual for running X/Twitter accounts via tical-code.
Any AI instance working on tical-code must read this before touching X account operations.

## System Architecture

### Two-Account Strategy

| Account | Role | Density | Content |
|---------|------|---------|---------|
| @primary_account (2010) | Main production | Low (2+8/day) | Long-form + Imagen images |
| @secondary_account (2025) | Practice/learning | Medium (6/day) | Text-only experiments |

Both are Premium verified. Both can earn 5M impression revenue sharing (biweekly payout).

### Components on Oracle VPS ([REDACTED])

```
Continuous Learning (never sleeps):
  x_continuous_agent.py (screen x-agent)
    - Every 5min: quick pulse (breaking news from Musk/Altman)
    - Every 15min: deep analyze (run x_pulse + x_viral_analyzer)
    - Every 60min: self-update strategy (adjust gates based on data)
    - ABSOLUTELY NEVER POSTS (POSTING_PROHIBITED = True)

Viral Mechanics Learning:
  x_viral_analyzer.py (runs even hours via continuous agent)
    - Studies what makes posts go viral (hook type, format, timing)
    - Tracks our own post performance (best/worst)
    - Detects breaking news in real-time
    - Stores patterns in x_viral_knowledge.json

Passive Reply (to @mentions):
  x_ai_worker.py v4.5 (screen x-worker)
    - Gate 0: evaluate if target post is worth replying to
    - Gate 1: Gemini generates meaningful reply, skip if not good enough
    - Gate 2: profile impact check
    - Also: proactive mutual follower scanning (every 5min)

Original Posting (cron, hard-limited):
  x_content_strategy.py - max 2/day, Imagen images (cron 0,4,8,12 UTC)
  x_smart_reply.py - max 8/day, short observations (cron 0,0,4,8,12,16,20 UTC)

Quality Control:
  x_quality_standards.md - canonical quality bible
  x_topic_seeds.json - post ideas fed by pulse scanner
  x_5m_goal.json - 5M impression target tracker
  x_viral_knowledge.json - viral mechanics patterns
  x_own_performance.json - our post analytics
  x_mutual_followers.json - mutual follower list
  Pulse reports in pulse_reports/
```

## Life-and-Death Rules

### Rule 1: Learning != Posting
- Learning: 24/7, high intensity, unlimited API calls
- Posting: strictly limited, low density, controlled by cron
- The continuous agent must NEVER post. Posting scripts are separate and cron-gated.

### Rule 2: Account Growth Tiers
- Tier 3 (current): 600-2k followers, max 2+8/day, cold recovery
- Tier 2: 2k-10k followers, 3-5+10-15/day, 5M goal in progress
- Tier 1: 10k-50k, 5-10/day, product promotion begins
- Tier 0: 50k+, unlimited, full product funnel
- Moving up tiers requires quality + time, not volume
- 5M impression milestone triggers platform revenue sharing = less restricted

### Rule 3: Reply Quality = Profile Quality
Every reply appears on your profile timeline.
- Gate 0: Is the TARGET post worth replying to? (<30 chars, spam, bait -> skip)
- Gate 1: Is our reply good enough? (specific, not generic, <220 chars)
- Gate 2: Would this reply make the account look better or worse?

### Rule 4: Mutual Followers Are Gold
- Reply to mutual followers' good posts = algorithm reward (reply chain signal)
- Scan every 5 minutes, max 2 replies per cycle
- Prioritize by follower count and content quality
- Never reply to low-effort or spam posts even from mutuals

## Target Metrics
- 5M impressions / 90 days per account
- Required pace: ~55k impressions/day
- Current tracking in x_5m_goal.json
- Revenue sharing: biweekly payout when target met

## Deployment Pattern (for new accounts)
1. Create X Developer Portal app (Read+Write permissions)
2. Get 5 credentials: API Key, API Secret, Bearer Token, Access Token, Access Token Secret
3. Save to anchor.json key_references.x_agent.{app_name}
4. Create x_config.json on target VPS (chmod 600)
5. Add cron entries (copy pattern from existing accounts)
6. Create screen workers (x_ai_worker.py + x_continuous_agent.py)
7. Test: one manual post, then let cron take over
8. Monitor: check logs for first 24 hours

## X API Limitations
- OAuth 1.0a can reply to mutual followers only (not to non-mutuals unless mentioned)
- Trends API requires Bearer Token (OAuth 2.0) - works for global/US
- organic_metrics gives impression/click data (Basic plan+)
- No API for Creator Studio UI features (inspirations, scheduling)
- verified field does NOT show X Premium subscribers (API bug/limitation)
- Reply settings may block replies even with everyone setting

## AI Agent Competitive Intelligence

### Monitored Accounts
- @moltbook (ID: 2016168172773376001) - AI agent, product/API focused, 500+ likes
- @aixbt_agent (ID: 1852674305517342720) - AI agent, mostly replies, low engagement
- @truth_terminal (ID: 1802642686710837249) - Famous AI, crypto-heavy audience

### Key Findings (May 2026)
1. AI agent accounts currently get 10-500 likes per post - modest engagement
2. Reply sections are predominantly HUMANS, not other AIs
3. @moltbook has the most legitimate AI presence (product/API announcements)
4. @truth_terminal's audience is heavily crypto-focused
5. @aixbt_agent operates mostly as a reply bot (low original post engagement)

### Strategic Implications
- The AI Agent space on X is still EARLY. Most accounts are small.
- Reply sections being human-dominated means there's OPPORTUNITY for AI-to-AI interaction that doesn't exist yet
- Our @primary_account and @secondary_account should observe and differentiate:
  - Don't be a crypto-focused AI agent (too crowded)
  - Focus on tech/AI analysis + tical-code product value
  - Build authentic human engagement first, AI-to-AI is future
- Monitor @moltbook's product approach: "GET /api/v1/home" type announcements work
