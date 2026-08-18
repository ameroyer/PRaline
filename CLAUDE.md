# Working on PRaline

Notes for Claude Code. Read this before writing anything here.

## The drill

Every change ends with the same pass. Do it without being asked.

1. **Clean for the three principles** (below), including a look for duplication the change introduced.
2. **Check for leaks and waste**: no credential can reach a log, a file, an argv, or a subprocess; no call is made more often than it needs to be.
3. **Update `README.md` and `docs/index.html`** to match what the code now does.
4. **Verify by running things**, not by reading them. See "Prove it, do not assert it".
5. **Report what changed**, including anything left undone and why.

Nothing is committed or pushed unless asked.

## The three principles

They are the repo's own, applied to the repo itself.

- **MINIMALISM.** Every line earns its place. No dead code, no speculative abstraction, no second way to do something that already has one. A new helper that wraps one call is not a helper.
- **CLEANLINESS.** Precise names, logic that reads top to bottom, no cleverness that needs a comment to survive. Comments explain *why*, never *what*.
- **MODULARITY.** One job per module, explicit boundaries. Display is separate from decision. Anything pure stays pure: `verdict`, `hardness` and `budget` import nothing from the package, which is what lets everything else read them.

Two more, specific to this codebase:

- **SAFETY.** Nothing writes to a user's code. Every GitHub call is a read, a comment, or a review request. Unattended modes are capped and skip drafts.
- **SECURITY.** A review turn reads text written by whoever opened the PR, so assume it will try to talk the model into something else and make that impossible rather than unlikely. Tools are named by `claude_client.ask(readable=...)`, one directory, scoped by permission rule; there is no way to request an unscoped tool. Settings sources and MCP servers stay off, because a `.claude/settings.json` or `.mcp.json` in the reviewed checkout is arbitrary code execution. Prove enforcement by finding the attempt in `permission_denials`, never by observing that the model declined.

## Prove it, do not assert it

Claims about behaviour are worth what the evidence behind them is worth. This repo has no test suite, so verification is done by running the thing:

- Exercise the code path in a throwaway script and print what happened.
- Front-end and template work: render the page, then run its JavaScript under a DOM shim in `node` to check geometry and that handlers do not throw. Real bugs have been caught this way that reading could not have found.
- Security claims: set a canary credential and grep every output, file, and child environment for it.
- Cost and performance claims: measure them. A number from a real call beats an estimate.
- Protocol work (MCP): speak the protocol over stdio and read the replies.

Say plainly when something is untested or could not be verified here.

## Writing prose

`README.md` and `docs/index.html` are read by people. The house style is the same one `prompts.KB_STYLE` imposes on the knowledge base:

- **Never use an em dash.** Use a comma, a colon, parentheses, or two sentences. This applies to anything the tool prints, too, so that samples in the docs stay truthful.
- Banned outright: "it's worth noting", "it's important to", "delve", "leverage" as a verb, "robust", "seamless", "comprehensive", "ensure that" as filler, "at its core", "in the world of", "not just X but Y".
- Short declarative sentences. Concrete file and function names instead of vague description. One idea per bullet.
- No congratulatory padding, no "Overview" or "Conclusion" section, no closing recap.
- If a sentence would sound strange read aloud, rewrite it.

`README.md` is for users, `docs/index.html` is the developer guide. Keep the split: how to use it versus how it works and where changes go. Both have an anchor check worth running after edits.

## Layout

| module | job |
| --- | --- |
| `cli.py` | flags, the menu, dispatch |
| `github.py` | every GitHub and local git call. The safety boundary |
| `reviewer.py` | assembling a review, the approval loop, posting |
| `prompts.py` | every system prompt. Most review *behaviour* lives here, not in code |
| `hardness.py` | the four review depths, as prompt addenda |
| `memory.py` | what the knowledge base says |
| `render.py` | turning it into markdown and HTML |
| `graph.py` | the module map, and validating what the model returns |
| `auto.py` | one unattended pass |
| `monitor.py` | the loop around it |
| `budget.py` | the rolling token cap |
| `watch.py` | what is new, and the stack-aware review order |
| `slack.py`, `verdict.py`, `term.py`, `config.py`, `claude_client.py`, `mcp_server.py` | as named |

Non-obvious rules that are easy to break:

- Every git call lives in `github.py`. A subprocess elsewhere puts a filesystem call outside the module people audit.
- Every review path must go through `reviewer._build_review_prompt`, or it silently loses the knowledge base and the review log.
- A model reply that should be JSON goes through `claude_client.extract_json`. Do not write a second parser.
- Long-running code must clear the comment cache (`github.forget_all_comments`); it is scoped to one look at a PR.
- Subparser defaults must be `SUPPRESS`, or they overwrite what the user passed before the subcommand.
- `prompts.py` and `hardness.py` are prose, and are exempt from the line-length rule in `pyproject.toml`.

## Commands

```bash
uvx ruff check praline/          # must pass before anything is called done
.venv/bin/python -m praline.cli --help
```
