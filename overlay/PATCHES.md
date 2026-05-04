# PATCHES

Any patches into upstream `openclaw/openclaw` code (i.e., any file
outside `overlay/`) MUST be documented here with rationale, and applied
by a build step rather than living as a committed diff against
`upstream/main`.

## Current patches

**None.** As of the rockie port (PR-rockie-1), every Pebble ML
adaptation is contained in `overlay/`, and
`git merge upstream/main` is expected to stay clean.

## Format for adding a patch

When a patch becomes truly unavoidable, append a section here with:

```
### <name> (<date>)

**Why we couldn't avoid it.** Two paragraphs minimum, citing the
specific upstream behavior we needed to change and why the overlay
mechanism couldn't accommodate it.

**Files touched.** Bullet list, `path/to/file` + 1-line summary per file.

**Patch file.** Path under `overlay/patches/<name>.patch`. The build
step in `overlay/tenant/start.sh` reads this and applies it before the
gateway starts.

**Upstream tracking.** Issue/PR link if we're trying to upstream the
change. If we're not trying, why not.

**Removal criteria.** What would have to be true upstream for us to
delete this patch.
```

## Discipline

- Patches are last-resort. First, see if the same goal can be achieved
  with a managed hook, an overlay config, or a Plugin SDK plugin.
- One patch per concern. Don't bundle.
- Every patch has an upstream-tracking issue.
- A patch that can't be removed within 6 months is a flag — we should
  probably switch to a real fork or contribute upstream.
