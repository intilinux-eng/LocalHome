# The version whoever uses it sees - at the bottom of the home page, in the
# admin's System status. Raised when it's worth telling: 1.0.1 for fixes,
# 1.1.0 for something new, 2.0.0 for a change the installer has to act on;
# CHANGELOG.md's Unreleased goes under it, and a tag vX.Y.Z on its commit.
# pyproject.toml says the same (tests/test_version.py).
__version__ = "1.3.0"

# Where this program's source is - linked at the bottom of the home page.
# LocalHome is under the AGPL-3.0: whoever runs a modified copy for others
# to use over the network must offer them its source, so a modified copy
# points this at its own (web.source_url in config.yaml).
# Empty while the repository is private - a link nobody else could open,
# so the page shows none. Made public: "https://github.com/intilinux-eng/LocalHome".
SOURCE_URL = ""
