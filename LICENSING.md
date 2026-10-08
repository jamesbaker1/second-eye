# Licensing

Second Eye is open source under the **GNU Affero General Public License,
version 3 only** (SPDX: `AGPL-3.0-only`). The full text is in `LICENSE`.

The same code is also available under a **commercial licence** from the
copyright holder, James Baker, for anyone who cannot or does not
want to accept the AGPL's terms. This is dual licensing: you choose one
licence or the other, and nothing here changes what the AGPL itself says.

This page explains the arrangement in plain English. It is not legal advice,
and where it and `LICENSE` differ, `LICENSE` governs. If your firm's decision
turns on how the AGPL applies to you, ask your own lawyer.

## Everyone: AGPL-3.0-only

Everything in this repository is licensed under AGPL-3.0-only. You may use,
study, change and share it, including commercially, on the AGPL's terms. The
"only" matters: it means version 3, not "version 3 or any later version".

There is no separate community edition and no feature held back in this
repository. If a future enterprise feature is ever licensed differently, it
will sit in its own directory with its own licence (see "Enterprise features"
below), and nothing outside that directory changes.

## What the AGPL asks of a firm

The AGPL is the GPL with one addition, section 13: if you **modify** the
program and people **interact with it over a network**, you must offer those
people the source code of your modified version. Second Eye is used by
email, which is a network interaction, so section 13 is the part to think
about.

**Running it unmodified, for your own lawyers.** Use it as published, in your
own Cloudflare and Anthropic accounts or anywhere else. The AGPL does not
require you to publish anything. Your documents, settings, playbook and data
are not "the program" and the licence never reaches them.

**Running a modified copy, for your own lawyers.** You may change it however
you like. Because your lawyers interact with it by email, section 13 requires
you to offer *them* the source of your modified version, for example with a
link in the help reply or the footer of its messages. In practice that means
the people inside your firm who use it, not the public. Nothing obliges you to
publish your changes on the internet. Many firms will want to contribute them
back anyway, so they do not have to carry them alone.

**Offering it to others.** If you run it as a service for people outside your
organisation (other firms, clients, the public), or you distribute it (ship an
image, an installer, or a product that includes it), the AGPL applies in full:
everyone who receives it or uses it over the network must be offered the
complete corresponding source of what you run or ship, under the AGPL. That
includes your changes and anything you combined with it into one program.

**What "the program" is.** This repository: the Python application in
`src/secondeye`, the Cloudflare Workers in `cloudflare/`, the agent definitions in
`agents/`, the skills in `skills/`, and everything else here. Anthropic's
models and platform, Cloudflare's platform and your firm's document system
are separate services the program talks to; their own terms govern them.

## The commercial licence

A commercial licence grants the same code on conventional proprietary terms,
without the AGPL's obligations. Ask for one if, for example:

- your firm's software policy does not allow AGPL components at all, whatever
  the use;
- you want to modify it and run the modified version without offering your
  changes to the people who use it;
- you want to embed it in, or sell it as part of, a product or service you do
  not license under the AGPL;
- you need a warranty, an indemnity, or contract terms the AGPL does not give.

Contact: open an issue titled "Commercial licence" (with no confidential
details) and the maintainer will reply with a private channel.

Dual licensing is only possible because one party can grant every line of
the code under both licences. That is why outside contributions need a signed
contributor licence agreement (`CLA.md`), and why this project does not take
on copyleft dependencies (the comments in `pyproject.toml` record, for
example, why it reads PDFs with pypdfium2 and pypdf rather than PyMuPDF,
which is AGPL).

## The hosted service

The copyright holder also runs Second Eye as a paid hosted service, a
separate deployment per firm, with support. A firm that uses the hosted
service needs no licence for the code at all: it is a customer of a service,
on the terms of its service agreement. The self-hosted path is documented in
`docs/it/self-hosted.md`, and paid support is available for it.

## Enterprise features

Features built in future for large firms only may be placed in a directory
named `ee/`, under a separate commercial licence stated in that directory.
There is no `ee/` directory today: everything in this repository is
AGPL-3.0-only.

If `ee/` is ever created:

- files in it will carry their own licence, and only files in it;
- the AGPL code outside it will still build, run and be useful without it;
- code already released under the AGPL stays available under the AGPL. A
  licence granted on a released version cannot be withdrawn.

## Trademarks

The AGPL licenses the code, not the name. "Second Eye" and any logo used
with it are not licensed under the AGPL or under the CLA.

You may say truthfully that your software is based on, derived from or
compatible with Second Eye. If you distribute a modified version, or offer
one to others as a service, give it a different name and do not present it
as Second Eye or as endorsed by this project. Running an unmodified copy
inside your own firm under the name is fine.

## Third-party components

The dependencies this project installs (Python packages from PyPI and npm
packages for the Workers) are under their own licences. The Python ones are
permissive except certifi, which is MPL-2.0, a file-level copyleft that
does not reach this code. The Docker image also installs LibreOffice from
Debian, under the MPL-2.0 and others. Each component's licence governs that
component. A new dependency under the GPL or AGPL would end the commercial
licence, so one is not accepted (`CONTRIBUTING.md`).

## Contributing

Contributions are accepted under the CLA in `CLA.md`, which lets the
copyright holder include them in both the AGPL and commercial editions. The
contributor keeps the copyright in what they wrote. `CONTRIBUTING.md` says
how signing works.

## Why is there a CLA?

Because the commercial licence pays for the project, and it could not exist
without one. Under the AGPL alone, everyone who contributed would hold the
copyright in their part, and a firm that cannot use AGPL code could only be
licensed the parts nobody else wrote. The CLA gives the copyright holder
the right to include your contribution in the commercial edition too.

A CLA that allows relicensing is also how some projects have moved from an
open-source licence to a closed one after a community built them up. That
concern is fair, so the CLA limits what it allows:

- **You keep your copyright.** The CLA is a licence to use your work, not a
  transfer of ownership. You can use your own code however you like.
- **Your code stays open source.** Section 4 of `CLA.md`: any version that
  includes your contribution must also be available under AGPL-3.0-only or
  another licence approved by the Open Source Initiative. It cannot end up
  only in a closed product.
- **Released code stays released.** Every version published under the AGPL
  remains under the AGPL for anyone who has it. That cannot be withdrawn,
  by this project or anyone else.

We use a CLA rather than the lighter Developer Certificate of Origin (DCO)
because a DCO only confirms that you have the right to submit the code. It
grants nothing beyond the AGPL, so a commercial licence could not include
your work.

Signing takes one comment on your first pull request, and covers every
later one.
