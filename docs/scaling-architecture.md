# tical-code Scaling Verification Architecture

## 1. Current Verification Model

```
User Request ──→ Worker Node ──→ Execute ──→ Owner Manual Verify ──→ Deliver
                   │                         ↑
                   └──→ Verifier ────────────┘
                   └──→ External Model (Cross-Verify) ──┘
```

- Owner as the sole ultimate verifier
- Worker + Verifier + External Model (Cross-Verify)
- Effective but not scalable — human bottleneck

---

## 2. Core Contradictions at Scale

| Contradiction | Description |
|---|---|
| **Verification Bottleneck** | Cannot manually verify each of 1000 users, but AI hallucinations don't decrease with more users |
| **Non-linear Cost Explosion** | Concurrency + long context + failure retry, triple compounding |
| **Trust Model Change** | Users need to trust the system, but shouldn't need to cross-verify themselves |

---

## 3. Phased Architecture

### Phase 1: Slow Growth (1-100 Users)

**Trigger**: Users ≤ 100, single Worker capacity sufficient

**Exit Criteria**: Daily requests > 1000, or manual sampling backlog can't be cleared same-day

```
User Request
   │
   ▼
┌──────────────┐
│  VerifyPipeline  │──── verified=false ──→ Manual Sampling Queue
│  + TruthReporter │──── confidence<0.7 ──→ ↑
│  + Cross-Model   │                         │
└──────┬───────────┘                         │
       │ verified=true                        │
       │ confidence≥0.7                       │
       ▼                                      │
   Normal Delivery                             │
                                              
  Critical Operations (data deletion/payment/identity changes)
       │
       ▼
  safe_modify ──→ Force Human Confirm ──→ Execute
```

**Layered Verification**:

- **Auto-Verify Layer**: VerifyPipeline + TruthReporter + Cross-Model Verify
- **Manual Sampling**: Only review items flagged as "suspicious" by the verification layer (`verified=false` or `confidence<0.7`)
- **Critical Operations**: Force human confirmation

**Cost Control**:

- 80% of requests use flash models, only trigger pro model when verify layer activates
- User conversation context TTL: auto-compress on timeout

---

### Phase 2: Medium Scale (100-1000 Users)

**Trigger**: Users > 100, or Phase 1 manual sampling backlog

**Exit Criteria**: Single queue throughput capped, or verification latency > 5s P99

```
User Request
   │
   ▼
┌──────────────┐     ┌─────────────────────┐
│   Executor    │────→│  Verifier Pool       │
│  (any model)  │     │  ┌───┐ ┌───┐ ┌───┐  │
└──────────────┘     │  │ V1│ │ V2│ │ V3│  │ ← Random selection, ≠ executor model
                     │  └───┘ └───┘ └───┘  │
                     └────────┬────────────┘
                              │
                     ┌────────▼────────────┐
                     │  Meta-Verifier       │ ← Verifies the verifiers
                     │  (Audit chain layer 3)│
                     └────────┬────────────┘
                              │
                     ┌────────▼────────────┐
                     │  Human (highest level only) │
                     └─────────────────────┘
```

**Verifier Pool**:

- Round-robin verifier pool, randomly selects different models
- Prevents self-verification by the same model
- Audit chain: Execute → Verify → Meta-Verify

**Tiered Trust Table**:

| User Operation | Verification Strength | Example |
|---|---|---|
| Read-only/Chat | Flash self-check | "What's the weather today?" |
| Generate Content | cross-verify | "Help me write code" |
| Critical Operations | cross-verify + human confirm | "Delete my data" |

---

### Phase 3: Explosive Growth (1000+ Users)

**Trigger**: Users > 1000, or Phase 2 verification latency > 5s P99

**Exit Criteria**: N/A (target architecture, continuously optimized)

```
                    ┌─────────────┐
                    │  User Request  │
                    └──────┬──────┘
                           ▼
                 ┌───────────────────┐
                 │   Message Queue   │ ← Peak shaving buffer
                 │ (Redis Streams)   │
                 └────────┬──────────┘
                          ▼
              ┌───────────────────────┐
              │     Worker Pool        │ ← K8s / serverless auto-scaling
              │  ┌────┐ ┌────┐ ┌────┐ │
              │  │ W1 │ │ W2 │ │ WN │ │
              │  └────┘ └────┘ └────┘ │
              └────────┬──────────────┘
                       ▼
              ┌───────────────────────┐
              │     Result Queue       │
              └────────┬──────────────┘
                       ▼
              ┌───────────────────────┐
              │   Verification Pool   │ ← Independent lightweight service
              │  ┌────┐ ┌────┐ ┌────┐ │
              │  │ V1 │ │ V2 │ │ VN │ │
              │  └────┘ └────┘ └────┘ │
              └────────┬──────────────┘
                       │
              ┌────────▼──────────────┐
              │  Tiered Routing        │
              │  ├ verified=true → Deliver │
              │  ├ suspicious → Escalate   │
              │  └ critical → Human        │
              └───────────────────────┘
```

**Key Design Principles**:

- Workers and verifiers are fully decoupled
- Workers auto-scale based on load (K8s / serverless)
- Verification pool is an independent lightweight service
- Message queue absorbs traffic spikes

**Cost Formula**:

```
Per-user cost = Execution Cost + Verification Cost

Verification rate is leverage:
  Chat → 10% verification rate
  Generation → 50% verification rate
  Critical → 100% verification rate

Total Cost = Σ(User_i × (exec_cost_i + verify_rate_i × verify_cost_i))
```

---

## 4. Key Insight

The pattern the owner discovered — "give AI output to a new AI for verification" — is fundamentally about **verification-execution isolation**.

At scale, this is automated, not manual:

```
1. Every AI output automatically enters the verification queue
2. Verifier assigned randomly, different model from executor
3. Verification result determines whether to escalate
4. Humans only handle highest-level issues
```

**TruthReporter is the minimum viable version of this system.**

---

## 5. Current Code vs Architecture Mapping

| Current Component | Maps To |
|---|---|
| VerifyPipeline | Phase 1 Auto-Verify Layer |
| TruthReporter | Phase 1-2 Audit Trail + Trust Degradation |
| Cross-Model Verify | Phase 2 Verifier Pool Prototype |
| safe_modify | Phase 1 Critical Operation Human Confirm Mechanism |
| anchor.json Immutable Rules | Cross-phase Security Baseline |

---

## 6. Unimplemented Gaps (Requires Follow-Up)

| Gap | Description | Priority |
|---|---|---|
| Message Queue | Redis Streams / RabbitMQ | Phase 3 Prerequisite |
| Worker Pool Auto-Scaling | K8s HPA / serverless | Phase 3 Prerequisite |
| Verifier Pool Management | Multi-model API routing | Phase 2 Core |
| Per-User Tiered Trust System | per-user trust level | Phase 2 Enhancement |
| Cost Monitoring Dashboard | Real-time cost tracking + alerts | Phase 1 Needed |
| Peak Rate Limiting & Degradation | Rate limiting + degradation + circuit breaker | Phase 2 Needed |
