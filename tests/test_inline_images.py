"""the inline-image guard refuses exactly the suffix mattermost cannot parse
(PRI-1225), and the accepted forms are the ones mattermost's own parser takes."""

import re

from mcp_mattermost.inline_images import malformed_image_suffixes, refusal_for

# mattermost webapp utils/markdown/helpers.ts parseImageDimensions, verbatim
MATTERMOST_DIMENSION_PATTERN = re.compile(r" =(\d*)(?:x(\d+))?$")


def test_refuses_the_dangling_x_that_broke_the_live_post():
    message = "prose\n\n![claim](https://zipline.prism-dynamics.org/raw/abc.png =640x)\n*claim*"
    assert malformed_image_suffixes(message) == ["https://zipline.prism-dynamics.org/raw/abc.png =640x"]
    assert "dangling x" in (refusal_for(message) or "")


def test_accepts_every_form_mattermost_parses():
    for suffix in (" =640", " =640x360", " =x360"):
        target = "https://host/a.png" + suffix
        assert MATTERMOST_DIMENSION_PATTERN.search(target), suffix
        assert refusal_for(f"![a]({target})") is None


def test_the_refused_form_is_exactly_what_mattermost_fails_to_parse():
    assert MATTERMOST_DIMENSION_PATTERN.search("https://host/a.png =640x") is None


def test_ignores_plain_links_and_unsized_images():
    assert refusal_for("see [the doc](https://host/x =640x) and ![a](https://host/a.png)") is None
