#!/bin/bash

# This script retrieves the version from a git repository and forms a file
# like:
#  Version: 0.1
#  Release: 45xt

# so that rpm builds can use that. The purpose is also that such a file can
# be stored in the SRPM to help later builds w/o the git repo.

GITDIR=.git/
REPONAME=$(basename $(pwd))
GIT_HEAD=HEAD

case "$1" in
	-d)
		GITDIR="$2"
		shift 2
	;;
	-n)
		REPONAME="$2"
		shift 2
	;;
	-r)
		GIT_HEAD="$2"
		shift 2
	;;
esac
	
if ! VERSION_STRING=$(git --git-dir="$GITDIR" describe --tags "$GIT_HEAD") ; then
	exit 1
fi

VERSION_HEAD=$(git --git-dir="$GITDIR" rev-parse "$GIT_HEAD")

VERSION_VERSION=$(echo $VERSION_STRING | \
	sed 's/^v\?\(.*\)-\([0-9]\+\)-g.*$/\1/;s/-//;s/^v//')

VERSION_RELEASE=$(echo $VERSION_STRING | \
	grep '\-g.\+$' | \
	sed 's/^v\?\(.*\)-\([0-9]\+\)-g.*$/\2/')

cat '-' <<EOF > $REPONAME-gitrpm.version
Version: $VERSION_VERSION
Release: $VERSION_RELEASE
Head: $VERSION_HEAD
EOF
#eof
