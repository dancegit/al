# Layer-local templates

A template here replaces the kit template of the same name for every command, hop or expert of this
layer that uses it: `commands/<template>.md`, `hops/<template>.md`, `experts/<file>`. It uses the same
`{{placeholders}}` as the kit's; an unknown placeholder is an error at `al gen`.

Make one with `al eject <layer> command|hop|expert NAME` rather than by hand, and see which templates
are yours, and whether the kit's version has changed since, with `al eject <layer> --list`.
