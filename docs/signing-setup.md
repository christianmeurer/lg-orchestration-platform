# Commit Signing Setup

All commits from the primary author are signed. To verify:

## Verify a signed commit

```bash
git log --show-signature -1
```

## Set up SSH-signed commits (recommended)

```bash
git config --global gpg.format ssh
git config --global user.signingkey ~/.ssh/id_ed25519.pub
git config --global commit.gpgsign true
```

Register your SSH key as a "signing key" (not just an auth key) in GitHub Settings → SSH and GPG keys → New SSH key → Key type: Signing Key.

## Set up GPG-signed commits (alternative)

```bash
gpg --full-generate-key  # RSA 4096 or Ed25519
gpg --list-secret-keys --keyid-format=long
git config --global user.signingkey <KEY_ID>
git config --global commit.gpgsign true
gpg --armor --export <KEY_ID>  # paste this into GitHub Settings → GPG keys
```

## Zenodo DOI

This repository is archived at Zenodo with a persistent DOI. See the badge in README.md.
