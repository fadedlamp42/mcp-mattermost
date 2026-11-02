"""refuse inline-image markdown that mattermost will silently break (PRI-1225).

mattermost sizes an inline image with a suffix inside the parentheses,
`![alt](url =640)` or `![alt](url =640x360)`, parsed by the webapp's
parseImageDimensions with the pattern ` =(\\d*)(?:x(\\d+))?$`. a suffix that
pattern does not match is NOT ignored: it stays glued to the url, the browser
requests `...png%20=640x`, the host answers its not-found page, and the post
shows a broken-image icon. nothing errors anywhere, and the server-side
metadata (fetched from the stripped url) even reports correct dimensions, so
the break is invisible until a human looks (caught live 2026-11-02).

the one shape that bit us is a width with a dangling `x` and no height
(` =640x`). checking it here, at the only door every sister's post goes
through, makes the broken form undeclarable for hand-written posts too, not
just for the tool that emits images.
"""

import re

# an inline image whose parenthesized target ends in " =<digits>x" with no
# height after the x: exactly the form parseImageDimensions fails to match
DANGLING_SIZE_SUFFIX = re.compile(r"!\[[^\]]*\]\((?P<target>[^)\s]+ =\d*x)\)")


def malformed_image_suffixes(message: str) -> list[str]:
    """every inline-image target in the message whose size suffix mattermost
    cannot parse; empty when the message is safe to post."""
    return [match.group("target") for match in DANGLING_SIZE_SUFFIX.finditer(message)]


def refusal_for(message: str) -> str | None:
    """an actionable refusal, or None when every inline image is well-formed."""
    broken = malformed_image_suffixes(message)
    if not broken:
        return None
    examples = ", ".join(target[-24:] for target in broken[:3])
    return (
        f"refusing to post: {len(broken)} inline image(s) end in a size suffix with a dangling x "
        f"({examples}). mattermost only parses ` =640` (width) or ` =640x360` (both); ` =640x` "
        "stays inside the url and renders as a broken image. use the markdown "
        "report_block_publish returned verbatim, or drop the trailing x."
    )
