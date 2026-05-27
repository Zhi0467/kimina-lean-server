# Aristotle Algorithm: Detailed Write-up and LeanFoundry Design Analysis

---

## Open Ambiguities

This section records questions where the Aristotle paper is silent or underspecified, and where our current write-up makes assumptions that should be flagged explicitly.

---

### A. Termination of the Outer Decomposition Loop

**The ambiguity**: Within MCGS, termination is well-defined. Soft termination: UCB scores decay as visit counts grow, naturally routing budget away from exhausted states. Hard termination: negation pruning marks a state DISPROVED and propagates "dead" upward. The paper does not describe an analogous termination mechanism for the outer loop — the level at which a particular decomposition (lemma list) is abandoned and either revised or discarded entirely.

In practice, multiple independent MCGS runs execute against a single decomposition. Each run is a separate search with its own budget. When all of them fail, the system is left with a collection of proved and unproved lemmas. The paper says this annotation is fed back to revise the decomposition (Section 2.2.2), but does not specify:

1. **How many MCGS runs per decomposition before triggering revision?** Is there a fixed count, a wall-clock budget, or a pass-rate threshold?
2. **How many revision iterations before the theorem is abandoned entirely?** The paper describes the revision loop but gives no termination criterion for it.
3. **Is there a hard-termination analog?** Within MCGS, negation pruning provides a definitive "this state is false" signal. There is no equivalent at the decomposition level — a decomposition that leads nowhere could be wrong, too coarse, or simply require more search budget. The system cannot distinguish these cases.

**Current assumption in this document**: we treat decomposition failure as producing no training signal (Section 8). This is correct as far as it goes, but it sidesteps the question of whether the outer loop has any mechanism analogous to hard termination that would let it actively conclude a decomposition strategy is unworkable. As written, the outer loop terminates only by exhausting a fixed revision budget, not by proving infeasibility.

**Why this matters for training**: the gradient signal for decomposition quality depends entirely on how much MCGS budget is allocated per decomposition before giving up. Too little budget and good-but-hard decompositions look identical to bad ones. Too much budget and the training loop is dominated by expensive failed searches. The paper's IMO evaluation used TTT and large parallelism to paper over this, but a principled training recipe needs an explicit budget allocation policy here.

---

### B. Training the Decomposition Capability

**The ambiguity**: the lemma generation steps (informal proof → lemma list → formalization) are separate LM calls outside the tactic search. The tactic-level RL signal therefore does not directly supervise decomposition quality. The paper does not describe a dedicated training objective for this capability.

**Current assumption in this document**: decomposition capability is acquired through pretraining, shaped by end-to-end RL (closed decompositions generate training examples; failed ones do not), and refined by TTT at inference time. This is plausible but not confirmed by the paper. In particular, it is unclear whether the informal reasoning model is the same model as the tactic policy (trained jointly) or a separate model (possibly frozen or fine-tuned separately). The paper's statement that "all three outputs — hidden CoT, inline comments, and formal tactics — are co-trained by RL" suggests a single model, but the lemma generation prompts are structurally different from tactic generation prompts and may involve a different inference mode.

---

### C. Bonded vs Unbonded Value Model

**The core design decision**: the value model can be in one of two configurations relative to the policy, and LeanFoundry needs to make this an explicit initialization parameter because the two configurations have different infra responsibilities.

**Bonded mode**: the value model and policy are the same checkpoint, distinguished by a task token in the prompt (as in Aristotle). LeanFoundry owns the full value training loop: label generation (positive, censored negative, disproved), warm-start from proof structure data before MCTS begins, and co-training alongside the policy. The `PolicyClient` interface handles both tactic sampling and value estimation. Cold-start in this mode: LeanFoundry bootstraps value labels from verified SFT proofs (every state on a known proof path is label=1; proof_depth/proof_size give weak ordering signal), giving the value model an initial prior before the first MCTS round. LCB selection in early rounds uses these weak labels; the estimates improve as MCTS generates real search traces.

**Unbonded mode**: the value model is external — owned, trained, and served entirely by the training repo. LeanFoundry does not know how the value model was trained or what its architecture is. In this mode LeanFoundry must:
1. Expose a separate `ValueClient` protocol (distinct from `PolicyClient`) that the training repo plugs in
2. Ensure `FactorizedProofState` is richly serializable — the external value model needs full goal expressions, local context, metavariable coupling groups, and tactic history to score a state
3. Call `ValueClient.estimate(states)` at each expansion, passing the full `FactorizedProofState` objects
4. Not generate or store value labels internally — this is entirely the training repo's concern

```python
# leanfoundry/interfaces.py

class ValueClient(Protocol):
    async def estimate(self, states: list[FactorizedProofState]) -> list[float]:
        """Score each state in [0, 1]. Called once per MCTS expansion."""
        ...

class PolicyClient(Protocol):
    async def sample_tactics(self, prompt: str, n: int, temperature: float) -> list[str]: ...
    async def sample_proofs(self, prompts: list[str], n: int, params: dict) -> list[str]: ...
    # NOTE: estimate_value removed from PolicyClient — moved to ValueClient
    # In bonded mode: BondedValueClient wraps the same model endpoint with a task token
    # In unbonded mode: training repo supplies any ValueClient implementation
```

**LeanFoundry initialization**:
```python
SearchEngine(
    policy_client=...,
    value_client=BondedValueClient(policy_client),  # bonded: same model, task token
    # OR
    value_client=training_repo.MyExternalValueModel(),  # unbonded: training repo owns
    value_mode="bonded" | "unbonded",
)
```

In bonded mode, LeanFoundry also owns the value label schema and the warm-start pretrain step. In unbonded mode, it does not write to `state_value_labels` at all — the training repo decides whether and how to log value training data.

**Cold start for LCB when value is unavailable**: regardless of mode, the first MCTS round may have no reliable value estimates. LeanFoundry needs a fallback LCB policy for this case: uniform random among sorry-states, or a heuristic based on goal expression complexity. This fallback is a parameter, not hardcoded. The choice is not neutral — it determines which lemmas get early budget and therefore which appear in training data first.

---

### D. TTT Data Isolation from the Main Replay Buffer

**The ambiguity**: Aristotle uses Test-Time Training (TTT) at evaluation time — retraining the model on its own search traces for a specific target theorem, iterating until the theorem is solved or budget is exhausted. The paper does not specify whether TTT-generated training data is isolated to the TTT loop or allowed to flow back into the main replay buffer.

This matters because TTT traces are highly distribution-shifted relative to the training distribution: they are traces from a model that has been fine-tuned on one specific theorem family. If these traces enter the main replay buffer, they could distort the general policy. If they are fully isolated, the TTT loop is a closed evaluation mechanism with no training-time analog.

**LeanFoundry implication**: TTT is listed as Extension C with an explicit trigger condition. The design doc does not specify whether TTT writes A-type data to the main Data Buffer or to a TTT-local buffer. This needs an explicit decision before Extension C is implemented: TTT should write to a separate, non-persistent store that is discarded after each theorem attempt, with only verified root proofs optionally promoted to the main buffer under a deduplication check.

---

### E. PolicyClient Scope for Informal Reasoning Calls

**The ambiguity**: the lemma generation pipeline (informal proof → lemma list → formalization) makes structurally different calls from tactic prediction — open-ended natural language generation with multi-turn reflection rather than conditioned tactic string prediction. Whether these go through the same `PolicyClient` as tactic prediction (same model, training repo owns prompt formatting) or through a separate client (different model, possibly frozen, not trained by LeanFoundry) is unspecified.

**This is downstream of the bonded/unbonded value decision but independent of it.** Both configurations are valid:

- **Same model via PolicyClient**: the training repo adds informal reasoning prompts to the same endpoint. LeanFoundry's `PolicyClient` is prompt-format-agnostic — `sample_proofs` already accepts arbitrary prompt strings. No new infra needed; the training repo's prompt builder owns the distinction between tactic prompts and informal reasoning prompts. Co-training informal and formal capabilities through the same RL loop is consistent with Aristotle's single-model claim.

- **Separate client outside LeanFoundry**: the training repo calls a different model (e.g., a larger frozen model via external API) for informal reasoning. LeanFoundry has no visibility into this. The infra boundary is clean; the cost is that informal reasoning capability is frozen and does not improve with training.

**Practical implication**: LeanFoundry does not need to add a new interface for informal reasoning calls — `PolicyClient.sample_proofs` with custom prompts covers the single-model case, and a fully external client outside LeanFoundry covers the separate-model case. The training repo declares which it is using; infra has no opinion.

---

## Train Repo vs Infra Repo: Responsibility Boundary

The infra repo (LeanFoundry) owns execution, search, replay, and evaluation. The training repo owns curriculum, rollout logic, outer loops, and experiment configuration. This section makes the boundary explicit for the two outer loop patterns most relevant to Aristotle-style training.

**Infra repo provides** (complete, no additions needed beyond G1 and the ValueClient split):
- `EnvBackendClient.verify`: typecheck any Lean block, including `:= by sorry` for statement-only checking
- `EnvBackendClient.step_batch`: stateless tactic execution
- `SearchEngine`: MCGS over AND/OR hypergraph, initialized from `TheoremSpec.initial_lean_block`; accepts both `PolicyClient` and `ValueClient` as injectable dependencies
- `PolicyClient`: tactic sampling and proof sampling (value estimation removed — see ambiguity C)
- `ValueClient` protocol: single method `estimate(states: list[FactorizedProofState]) -> list[float]`; `BondedValueClient` wraps `PolicyClient` with a task token for bonded mode; training repo supplies its own implementation for unbonded mode
- `Data Buffer`: durable A-type and B-type storage; `state_value_labels` written by LeanFoundry only in bonded mode
- `ProofTree.roots`: per-sorry proved/unproved status after search

**Training repo owns** (rollout classes implementing `BaseRollout`):

### Lemma Decomposition Rollout (Aristotle-style)

The full outer loop is a training repo rollout class:

```python
class LemmaDecompositionRollout(BaseRollout):
    async def generate(self, theorem_batch):
        for theorem in theorem_batch:
            # 1. Generate informal proof + lemma chain (LM calls, training repo logic)
            informal_proof = await self.policy.sample_informal(theorem.informal_statement)
            lemmas = await self.policy.decompose_to_lemmas(informal_proof)
            lean_sigs = await self.policy.formalize_lemmas(lemmas)

            # 2. Typecheck lemma statements (infra call, no new endpoint)
            for sig in lean_sigs:
                result = await self.env_backend.verify(sig + " := by sorry")
                # relay errors back, request corrections (training repo logic)

            # 3. Build initial_lean_block and hand to SearchEngine (infra)
            block = build_sorry_block(lean_sigs, theorem.lean_statement)
            spec = TheoremSpec(lean_statement=theorem.lean_statement,
                               initial_lean_block=block)
            proof_tree = await self.search_engine.run(spec, budget=self.budget)

            # 4. Read results, decide revision or abandon (training repo logic)
            proved = [r for r in proof_tree.roots if r.proved]
            unproved = [r for r in proof_tree.roots if not r.proved]
            if unproved and self.revisions_remaining > 0:
                # annotate and revise — outer loop termination is training repo policy
                ...
```

The infra has no visibility into steps 1–2 or the revision decision in step 4. It only executes step 3.

### Self-Guided Self-Play Rollout (SGS-style, Bailey et al. 2026)

SGS (arXiv:2604.20209) introduces three roles: Solver, Conjecturer, and Guide. The Conjecturer generates new theorem statements for the Solver to prove. The Guide scores conjectured theorems by relevance to unsolved targets and naturalness, preventing Conjecturer collapse to degenerate problems. This maps cleanly to a training repo rollout:

```python
class SGSRollout(BaseRollout):
    async def generate(self, unsolved_targets):
        # Conjecturer: generate candidate theorems near unsolved targets
        # (training repo LM call — no new infra needed)
        candidates = await self.policy.conjecture(unsolved_targets, n=self.n_conjectures)

        # Guide: score by relevance and naturalness
        # (training repo LM call — no new infra needed)
        scores = await self.policy.guide_score(candidates, targets=unsolved_targets)
        filtered = [c for c, s in zip(candidates, scores) if s > self.guide_threshold]

        # Typecheck conjectured statements (infra call — existing verify endpoint)
        valid = []
        for c in filtered:
            result = await self.env_backend.verify(c.lean_statement + " := by sorry")
            if result.success:
                valid.append(c)

        # Add valid conjectures to B-type pool (training repo writes to Data Buffer)
        await self.data_buffer.insert_b_type(valid)

        # Solver: run MCGS on conjectures + original unsolved targets (infra)
        all_targets = valid + unsolved_targets
        for theorem in all_targets:
            proof_tree = await self.search_engine.run(
                TheoremSpec(lean_statement=theorem.lean_statement),
                budget=self.budget
            )
            # collect A-type data from successful searches (standard EI loop)
```

**What SGS requires from infra**: nothing beyond what already exists. The Conjecturer and Guide are LM calls owned by the training repo. The only infra touch point is `EnvBackendClient.verify` for typechecking conjectured statements — the same endpoint already used for lemma statement checking. The Solver is the existing MCGS. No new LeanFoundry interfaces needed.

**The one open question for SGS in LeanFoundry**: the Guide scores theorems by "relevance to unsolved targets." This relevance signal requires the training repo to maintain a representation of which theorems are unsolved. The Data Buffer's `pass_rate_current` field on the `theorems` table is exactly this signal — the training repo reads it to identify unsolved targets and passes them to the Conjecturer. No infra change needed; this is a training repo curriculum decision.

---

## Part I: Aristotle Algorithm — Full Technical Write-up

### 1. The Core Search Problem

Aristotle must find a sequence of Lean 4 tactics that transforms a root theorem statement into an empty goal set — a state Lean's kernel accepts as a complete proof. The challenge is that the action space (possible Lean tactics) is enormous and unstructured, proofs can be dozens of steps deep, and individual steps can branch into multiple independent subgoals. Aristotle addresses this with **Monte Carlo Graph Search (MCGS)** over an AND/OR hypergraph.

---

### 2. States and Actions

**A state** is a Lean proof state: a tuple of (goal expressions, local context expressions, local variable names). Two states are considered equivalent — and merged into the same node in the search graph — if and only if all three components match exactly. Variable names are part of the key: `hP : P ⊢ Q` and `h1 : P ⊢ Q` are different nodes. This is a pragmatic choice that avoids expensive normalization.

**An action** is a text string — one or more Lean tactics, optionally with inline natural-language comments. Applying an action to a state via the Lean REPL can produce:
- An error (action is pruned)
- A single new state (one remaining subgoal)
- Multiple new states (e.g., after `cases`, `constructor`, or explicit `sorry` placeholders)

When an action produces multiple states, those states are AND-children: all of them must be proved for the action to succeed. This creates a **hypertree** structure. When states are deduplicated across different proof paths, the hypertree becomes a **hypergraph** — the search structure Aristotle calls MCGS.

Actions are deduplicated by the state transitions they produce: two tactic strings that map the same input state to the same set of output states are treated as the same action.

**Initialization from a sorry-block and the sentinel AND node**: When a Lean block containing multiple `sorry` placeholders is executed, the REPL produces one goal state per sorry. These states must all be proved for the theorem to be solved — they are AND-children. In the hypergraph, this is represented by treating the block execution itself as the root AND node: the root is an OR node (the theorem statement), and "execute initial block" is its single initial action, which is an AND node whose children are the sorry-states (OR nodes). No new node type is required — this fits the existing AND/OR structure exactly. The block-execution action is simply injected rather than policy-sampled, and since it is the only action at the root initially, UCB selection is trivial there. LCB then governs which sorry-state child receives budget, as with any AND node. If the outer iteration loop later injects a revised lemma block as a second root action, the root OR node now has two AND-node children competing under UCB — the search naturally compares proof strategies without any special casing.

**The critical state-splitting rule**: after an action produces multiple goals, those goals are split into independent states only if none of them contain **metavariables** — unresolved holes in Lean's proof term that different goals may share. If metavariables are present, goals remain bundled as a single state because choosing a value for the metavariable in one goal affects what the other goal is. Goals created by explicit `sorry` placeholders are always split and never permitted to have metavariables.

---

### 3. AND/OR Structure and Selection

The hypergraph is an AND/OR graph:
- A **state (OR node)** is proved if ANY single action applied to it succeeds.
- An **action (AND node)** succeeds only if ALL of its child states are proved.

Each MCTS iteration is strictly sequential: select → expand → backpropagate → repeat. The next iteration's selection depends on backpropagation from the current one; the cycle cannot be parallelized across iterations for a single theorem. Two selection mechanisms operate at different levels of the traversal:

**UCB over actions**: at a given state, select the action with the highest Upper Confidence Bound:

```
UCB(a) = value_estimate(a) + c * prior(a) * sqrt(parent_visits) / (1 + action_visits)
```

The prior `prior(a)` is the **empirical frequency** of this action in samples from the policy model, not the sequence log-probability. Log-probabilities penalize actions with multiple equivalent textual representations and are biased.

**LCB over AND-children**: when the selected action has multiple child states (e.g., Goal 1 and Goal 2 after a `cases`), descend into the child with the **lowest Lower Confidence Bound** — the hardest, most uncertain subgoal. Since all AND-children must be proved, the bottleneck governs the action's success. Budget therefore flows primarily to Goal 1. Goal 2 receives budget only in two cases: Goal 1 is proved (Goal 2 becomes the new bottleneck), or a later iteration from the root selects a completely different action that does not involve Goal 1 at all.

LCB and UCB are independent mechanisms. LCB operates at the parent AND-node level — which AND-child to descend into. UCB operates within the selected state — which action to try next from that state.

---

### 4. Budget, Termination, and Negation Pruning

**Soft termination**: as iterations accumulate on a difficult state without success, action visit counts grow and UCB scores decline. The PUCT formula routes budget away from exhausted states naturally.

**Hard termination via negation pruning**: for each single-goal state with goal `⊢ P`, a synthetic action is added to the state's action pool: prove `¬P` (i.e., `P → False`) in the same local context. This action competes normally under UCB selection alongside all other candidate tactics. Because UCB can select the negation action across many iterations with different tactic attempts each time, it receives a full sub-search — not a one-step check. If the negation is eventually proved, the state is marked **DISPROVED**. Propagation then sends "dead" upward through all parent AND-nodes containing this state: an AND-node with any DISPROVED child can never succeed and is eliminated from the parent OR-node's options.

**Proof extraction**: when the root state is proved, the system extracts the **proof subgraph** — an acyclic subgraph with one action per state, all AND-children of each selected action, and leaves that are empty goal sets. Postprocessing applies linter suggestions, removes redundant tactics by re-execution, and condenses chains of basic tactics into single automation calls. The final output is a self-contained Lean 4 file verified independently by the Lean kernel.

---

### 5. Parallelism in Search

The MCTS iteration cycle is strictly sequential within one theorem. The only parallelism within a single iteration is at the **expansion step**: N candidate tactics are generated together in one batched policy call, executed together in one `step_batch` call to Kimina, and their resulting states are valued together in one batched value call. This tactic-level batch parallelism is the full extent of within-theorem concurrency.

Across theorems, independent MCGS instances are trivially parallel — no shared state, no coordination required.

The practical batching challenge is at the cross-theorem level: when many theorem searches run concurrently, their policy and Kimina calls arrive in irregular bursts. A central dispatcher aggregating requests across concurrent searches before firing batched calls to the inference engine and Kimina would improve GPU and CPU utilization. The design of this dispatcher (batch window, queue depth, backpressure) requires empirical data from training runs and is a **v0 open problem**.

---

### 6. Policy and Value: One Model, Three Outputs

Aristotle uses a single large transformer (>200B parameters for IMO evaluation) for both action generation and state evaluation, distinguished by a task token in the prompt.

**When generating an action**, the model receives the current Lean proof state, the action history (all tactics taken to reach this state), an informal proof if available, and a task token. It first produces a **hidden chain of thought** (thinking tokens, inaccessible to future prompts), then outputs the action string: **inline natural-language comments** followed by formal Lean tactics. The comments persist in the proof as part of the action history and appear in future prompts — they are external persistent memory across the proof. The hidden CoT is ephemeral per-call scratchpad.

All three outputs — hidden CoT, inline comments, and formal tactics — are co-trained by RL. Comment production is enforced early in training by a formatting constraint; later the RL signal alone sustains it. The **dynamic CoT budget** is trained implicitly: only thinking traces that preceded a successful action are used as training targets, teaching the model to suppress CoT for trivial goals.

**When evaluating a state**, the same model with a different task token outputs a scalar in [0,1].

---

### 7. Reinforcement Learning: Expert Iteration

**Policy training**: each (state, action) pair in any found proof subgraph is a supervised example. The full sequence trained on is: Lean state + action history + optional informal proof → CoT tokens + inline comment + tactic text.

**Value training**: proved states receive label 1. States extensively searched but never proved are **censored negatives** — label 0 at reduced weight, because "not proved by this policy at this budget" is not the same as mathematically false. States marked DISPROVED via negation pruning receive label 0 at full weight — they are definitively false. The schema must distinguish these two kinds of negatives.

**Hindsight Experience Replay (HER)**: every proved non-root state in a proof subgraph is re-rendered as a standalone root theorem with its sub-proof. This multiplies training examples at zero additional Lean cost, with reported amplification of 10–50×. Candidates must pass Kimina re-verification before entering training. Two multipliers are tracked separately: `her_candidate_multiplier` (candidates per root proof) and `her_verified_multiplier` (verified candidates per root proof), because the gap between them is a key diagnostic.

**Trajectory representation**: a proof with AND-splits is a tree, not a flat sequence. The canonical representation is:

```python
@dataclass
class TacticNode:
    state: FactorizedProofState
    tactic: str
    children: list["TacticNode"]  # AND-children after split; single child for linear step

@dataclass
class ProofTree:
    roots: list[TacticNode]   # one per sorry in initial block
    rendered_lean: str         # canonical text from Mode A prefix
```

This one representation serves all three uses: `rendered_lean` is the Lean proof text for final verification; depth-first traversal of the tree yields `(state, tactic)` pairs for tactic-policy SFT; any proved subtree is a HER candidate. The tree structure must be preserved in the trajectory — flattening to a sequence loses the structural information needed to reconstruct Lean's branching syntax (`case inl =>`, `case inr =>`).

---

### 8. The Outer Lemma Pipeline

The search algorithm is most powerful when embedded in a higher-level informal reasoning loop. For each target theorem:

1. An LM generates an **informal proof** in natural language.
2. That proof is restructured as a **sequence of lemmas**, each individually simple enough for MCGS to close directly.
3. Lemma statements are **formalized** into Lean 4 type signatures.
4. Formalizations are submitted to the REPL; error messages are relayed back and corrections requested.
5. The resulting Lean block (sorry-d lemmas plus the main theorem) is handed to MCGS.
6. If the attempt fails, proved/unproved annotation is fed back to revise the decomposition.

**Initialization with multiple sorrys**: Lean handles dependencies automatically. When the block is executed, L2's proof state already has L1 available as a declared theorem regardless of whether L1 is sorry'd — sorry is transparent to subsequent declarations. MCGS works on all sorry states simultaneously; correctness of dependencies is enforced at final `/verify` time.

**How decomposition capability is trained**: The lemma generation steps (informal proof → lemma list → formalization) are separate LM calls, not tactic steps in the search. The model's decomposition capability therefore cannot be trained directly by the tactic-level RL signal. It is acquired and refined in three ways:

1. **Pretraining**: the base LLM sees mathematical texts where theorems are routinely proved via intermediate lemmas. This gives an initial prior over what useful decompositions look like.

2. **End-to-end RL signal**: when a decomposition leads to lemmas that MCGS successfully closes, the full trace — including the informal proof and lemma list that seeded the search — becomes training data for the policy. Decompositions that do not lead to closed proofs produce no training signal. Over many iterations, the model learns to generate decompositions that are actually provable by MCGS, not just plausible-sounding ones. The faithfulness judge tightens this further by filtering out formal proofs misaligned to their informal counterparts, ensuring the model cannot exploit a mismatch between the informal decomposition and the formal proof that actually closed.

3. **TTT at inference time**: for a specific hard theorem, TTT lets the model rapidly refine its decomposition style by retraining on traces from its own previous attempts. Lemmas proved in one attempt become context for the next, and the model learns which decomposition granularity MCGS can handle for this theorem family.

The practical implication for training infrastructure: decomposition quality is not directly observable as a per-call reward. The only observable signal is whether the downstream MCGS closed the lemmas. This means decomposition capability is sensitive to the difficulty distribution of the theorem dataset — if MCGS can close any lemma regardless of decomposition quality, the decomposition model receives no informative gradient. The training dataset must include theorems hard enough that decomposition granularity genuinely determines whether MCGS succeeds.

Each pipeline step consists of multiple sub-queries allowing the LM to reflect and revise. The process is noisy but produces enough correctly formalized useful lemmas to substantially outperform bare MCGS.

---

### 9. Test-Time Training

At scale, Aristotle runs iterative self-improvement at inference time: search, retrain on traces, reload, repeat. This allows the model to learn novel Mathlib APIs from initial explorations and enables cross-pollination between lemmas. TTT is an evaluation-time technique for olympiad-level targets, not a training-time loop.

---

## Part II: How This Algorithm Guides LeanFoundry's Design

### What LeanFoundry Gets Exactly Right

**Stateless REPL workers (P2)** addresses Aristotle's most expensive practical failure. Their v1/v2 anti-pattern was connection pinning: each theorem pinned to a specific REPL process, destroying load balancing and making preemption catastrophic. The v3 fix — inverted connection pattern, per-message routing with a global queue, state portability via zstd-compressed blobs — is correctly reproduced in both Kimina-MCTS and Extension A's C++ router.

**MCGS with AND/OR UCB/LCB** (Section 3.2) correctly ports Aristotle's search logic: empirical prior over log-prob, LCB-based hardest-child selection, and gated graph merging (tree in v0.1–v0.2, graph in v0.3 after a correctness acceptance test).

**State equivalence is gated, not assumed** (Section 3.1). Tactics like `aesop` use global state that can violate the deduplication assumption. The tree-only baseline in early versions, with graph merging gated behind a pass/fail test, is the right discipline.

**HER with reverification** (Section 3.4) correctly requires that HER candidates pass Kimina `/verify` before entering the training set. Tracking `her_candidate_multiplier` and `her_verified_multiplier` separately is right — the gap between them is a key diagnostic.

**Censored negative value labels** (Section 3.5) correctly captures the statistical subtlety: a state not proved is not mathematically false. The minimum-effort threshold and downweighted loss are correct mitigations. The schema should additionally distinguish negation-pruned states (hard label=0, definitively false) from effort-exhausted states (censored label=0).

**The 4-stage training pipeline** reflects the correct dependency structure: SFT cold-start (learn Lean syntax), whole-proof EI (cheap), MCTS EI (hard theorems), GRPO (sharpen after MCTS bootstraps nonzero pass rates). Prohibiting GRPO at pass_rate=0 is correct.

**TTT as Extension C** and **explicit trigger conditions for all extensions** (P9) prevent premature complexity, matching Aristotle's own incremental infrastructure development.

---

### Where LeanFoundry Generalizes Correctly

**Multiple search modes behind a common interface** — direct sampling, beam search, best-first, PUCT tree MCTS, MCGS, AND/OR MCGS — with algorithm as a parameter rather than a hardcoded identity. Whole-proof systems (Goedel-Prover), step-level systems (DeepSeek-Prover), and Aristotle-style MCGS all plug in via PolicyClient and EnvBackend.

**Self-correction as a first-class format** is motivated by Goedel-Prover-V2's ablations and is a correct generalization beyond Aristotle.

**env_profile versioning** is stricter than Aristotle describes and correct. Different Mathlib commits produce different tactic behaviors; cross-commit mixing silently corrupts the replay buffer.

**The infra/training split** with one-way dependency is architecturally clean and real, not nominal.

---

### The Lemma Pipeline: Training Repo Concern, Not Infra Gap

The lemma decomposition pipeline is a **training recipe pattern, not a framework component**. Aristotle uses informal proof revision; DeepSeek-Prover-V2 uses structured recursive subgoal decomposition. Both are different rollout classes in the training repo. The infra does not need to know which is running.

The training repo can implement lemma decomposition almost entirely with existing LeanFoundry interfaces:

- **LM orchestration** (informal proof → lemma chain → formalization → error correction): sequences of `PolicyClient.sample_proofs` calls with custom prompts. Pure training repo logic.
- **Syntax checking** of lemma statements: append `:= by sorry` and call `EnvBackendClient.verify`. No new infra endpoint needed — Lean type-checks the statement even with a sorry body.
- **Outer iteration loop** (annotate proved/unproved, revise): reads `ProofTree.roots` status, builds the revision prompt. Training repo orchestration.

The only genuine infra addition needed is one backwards-compatible field:

```python
@dataclass
class TheoremSpec:
    lean_statement: str
    env_profile: str
    header: str
    informal_statement: str | None = None
    initial_lean_block: str | None = None   # enables sorry-initialized code blocks
```

When `initial_lean_block` is set, SearchEngine executes the block through Kimina, extracts all sorry goal states, and initializes MCGS from them as an AND-hypergraph root. When None, behavior is unchanged. The field is algorithmically neutral — the infra does not know whether the sorrys came from lemma decomposition, a hand-written proof sketch, or anything else. Both Aristotle-style decomposition and DeepSeek-Prover-V2-style subgoal generation fit this interface.

The informal proof context used to condition the policy during search belongs in the training repo's prompt builder, not in the PolicyClient interface. The training repo decides what goes in the prompt string passed to the policy; the infra passes it through without inspection.

---

### Remaining Gaps

**Negation pruning is absent.** Adding it requires SearchEngine to synthetically construct `¬P` for each new single-goal state and add it to the action pool. If the negation action succeeds across its sub-search, propagation sends "dead" upward through parent AND-nodes. The `state_value_labels` schema needs a `disproved: bool` field to distinguish this from censored negatives. This is a SearchEngine addition.

**Inline comments must be preserved in the trajectory format.** The training data must record the full action string including inline comments, not just the tactic text. If SFT data strips comments, the model never learns to produce them and loses the persistent memory mechanism.

**Dispatcher for cross-theorem batching is v0 / needs research.** When many theorem searches run concurrently, their policy and Kimina calls arrive in irregular bursts. A central dispatcher collecting requests across concurrent searches before firing batched calls to SGLang and Kimina would improve utilization. The right design requires empirical data from Phase -1 profiling.

---

### Assessment: Is LeanFoundry Over-Specified?

**The infrastructure repo is not over-specified.** The abstraction boundaries — PolicyClient, EnvBackend, RLEngineBase, SearchEngine as a parameterized type — are clean and genuinely swappable.

**The training repo is specific but correctly framed** as one concrete demonstration. Qwen3.6-27B, Slime/Megatron/SGLang, and pass-rate bucketing thresholds are heuristics owned by the training repo, changeable without touching the infra.

**The scope boundary is appropriate**: LeanFoundry is explicitly a framework for step-level tactic search. This is a reasonable and explicit scope decision. The lemma pipeline fits naturally into the training repo as a rollout class, and the single infra addition (`initial_lean_block`) is small and general enough to support Aristotle-style, DeepSeek-Prover-V2-style, and any future decomposition approach without coupling the infra to any one of them.
