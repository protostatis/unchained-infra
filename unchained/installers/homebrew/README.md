# Homebrew cask

`Casks/unchained.rb` installs the same notarized dmg `/desktop` serves. Homebrew needs a
tap repository to read it from, so publish this directory as `homebrew-unchained` (a public
repo whose root is `Casks/`):

```sh
brew tap <owner>/unchained
brew install --cask unchained
```

## Bumping a release

The cask pins `version` and `sha256`, so it moves with each build:

1. `shasum -a 256 dist/Unchained-<version>-arm64.dmg`
2. Update `version` and `sha256` in `Casks/unchained.rb`.
3. Commit in the tap (not here); this file is the source of truth to copy from.

If a tap is not worth maintaining yet, `brew install --cask <url-to-dmg>` also works
directly against the download endpoint.
