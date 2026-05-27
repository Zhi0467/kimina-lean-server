author: Zhi 

I keep some dev notes here.

SearchEngine / rollout
    ├── PolicyClient  → SGLang / Slime model endpoint
    ├── ValueClient   → value model endpoint, maybe same SGLang service
    └── EnvClient     → Kimina/Pantograph Lean backend

  So for one expansion:

  1. SearchEngine selects a proof-state node.
  2. It asks PolicyClient for candidate tactics or an initial proof draft.
  3. It turns the model text into either:
      - tactics[] for /exec/step_batch, or
      - a Lean block with sorrys for /exec/init_batch.
  4. It sends that to the backend through EnvClient.
  5. Backend returns checked successor states / errors / new state_tokens.
  6. SearchEngine asks ValueClient to score resulting states.
  7. SearchEngine updates the AND/OR graph.

  So the backend is only the Lean execution service. It does not know about policy, value, MCTS, AND nodes, or training. The SearchEngine is the place where model outputs become backend
  requests and backend results become graph updates.