# Contributing

Thanks for taking the time to help.

## Bugs and ideas

- **Bugs**: open an issue with the *Bug report* form — what you did, what you expected, what happened, and the version
  (`beom-workbench --version` or the version label in the app). Logs or screenshots help; remove anything private first.
- **Ideas and feature requests**: open an issue with the *Feature request* form.
- **Questions**: use [Discussions](https://github.com/ParkBeomsoo/beom-workbench/discussions).
- **Security problems**: never in a public issue — see [SECURITY.md](SECURITY.md).

## Pull requests

Pull requests are welcome as **suggestions**, but they are not merged as they are. The [LICENSE](LICENSE) does not allow
modified versions; it does allow the changes you make only to submit an issue or pull request, and the maintainer applies
accepted changes. Small, focused pull requests with a clear description are the easiest to pick up.

## How changes reach `main`

- `main` holds releases; each release is tagged `vX.Y.Z`.
- Work happens on short-lived `feature/…` or `fix/…` branches created from `main`.
- A branch is tried on `develop` (the experiment branch) and applied by merging that same branch into `main` through a pull
  request with CI on Windows, macOS and Linux. `develop` itself is never merged into `main`; a required check enforces this.
