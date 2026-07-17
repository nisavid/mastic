# Issue tracker: GitHub

Issues and PRDs for this repository live in GitHub Issues at `nisavid/mastic`.
Use the `gh` CLI for tracker operations.

## Conventions

- Create: `gh issue create --repo nisavid/mastic --title "..." --body-file <path>`
- Read: `gh issue view <number> --repo nisavid/mastic --comments`
- List: `gh issue list --repo nisavid/mastic --state open`
- Comment: `gh issue comment <number> --repo nisavid/mastic --body-file <path>`
- Label: `gh issue edit <number> --repo nisavid/mastic --add-label "..."`
- Close: `gh issue close <number> --repo nisavid/mastic`

## Pull requests as a triage surface

**PRs as a request surface: no.**

GitHub shares one number space across issues and pull requests. Resolve an
ambiguous number with `gh pr view <number>` and then
`gh issue view <number>`.

## Skill routing

When a skill says to publish to the issue tracker, create a GitHub issue. When
a skill says to fetch a ticket, read the issue and its comments.

## Wayfinding operations

- The map is one `wayfinder:map` issue.
- Tickets are native sub-issues in map order.
- Dependencies are native GitHub issue dependencies.
- An open frontier ticket has no open blocker and no assignee.
- Claim with
  `gh issue edit <number> --repo nisavid/mastic --add-assignee @me`.
- Resolve at most the number of tickets authorized by the active work
  contract.
- Record the accepted answer in the ticket, close it, and append only a
  concise context pointer to the map.
- Use database IDs, not issue numbers or node IDs, when creating native
  dependency edges through `gh api`.
