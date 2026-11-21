"""AgentTrace live dashboard: a thin, read-only view over the store.

Phase 2, built under the Critic's five hard conditions (see README):
read-only absolutely, store-only data through agenttrace.query, no
platform features, one static page + stdlib-only server, honest
liveness (it tails a store that real runs write to; it simulates
nothing).
"""
