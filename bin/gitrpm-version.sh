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
OUTDIR="."
BOOTSTRAP=
GET_ONLY=n
DO_ZEROEXTRA=
UGLY_REGEXP='^v\?\([0-9\.]*\)-\?\([a-z]\+[0-9]*\)\?-\([0-9]\+\)\(-g.*\)\?$'

while [ -n "$1" ] ; do
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
	-C)
		OUTDIR="$2"
		shift 2
	;;
	-b)
		BOOTSTRAP="$2"
		shift 2
		;;
	-B)
		BOOTSTRAP="$2"
		GET_ONLY=y
		shift 2
		;;
        -S)
                # OpenSSL versioning: "v1.2.3f" ...
                UGLY_REGEXP='^v\?\([0-9\.]*\)-\?\([a-z]\+[0-9]*\)\?-\([0-9]\+\w\)\(-g.*\)\?$'
                shift 1;
        ;;
	-0)
		DO_ZEROEXTRA=y
		shift 1
		;;
esac
done

# Some quirks for projects not following clean "v1.2.3" versioning

case "$REPONAME" in 
    openssl)
        UGLY_REGEXP='^v\?\([0-9\.]*\w\?\)-\?\([a-z]\+[0-9]*\)\?-\([0-9]\+\)\(-g.*\)\?$'
        ;;
esac

	
if [ "$GET_ONLY" != "y" ] && [ -d "$GITDIR" ] ; then
	if ! VERSION_STRING=$(git --git-dir="$GITDIR" describe --tags --match 'v[0-9]*.[0-9]*' "$GIT_HEAD") ; then
		echo "There must be a tag in the form 'v0.1[.5]' for each version to consider." >&2
		exit 1
	fi

	VERSION_HEAD=$(git --git-dir="$GITDIR" rev-parse "$GIT_HEAD")

	VERSION_VERSION=$(echo $VERSION_STRING | \
		sed "s/$UGLY_REGEXP/\1/;s/-//;s/^v//")

	VERSION_RELEASE=$(echo $VERSION_STRING | \
		grep '\-g.\+$' | \
		sed "s/$UGLY_REGEXP/\3/")

	VERSION_EXTRA=$(echo $VERSION_STRING | \
        grep '\-g.\+$' | \
		sed "s/$UGLY_REGEXP/\2/" )
		
cat '-' <<EOF > "$OUTDIR"/$REPONAME-gitrpm.version
Version: $VERSION_VERSION
Release: $VERSION_RELEASE
Extra: $VERSION_EXTRA
Head: $VERSION_HEAD
EOF

fi

if [ -n "$BOOTSTRAP" ] ; then
	if [ ! -r "$OUTDIR"/$REPONAME-gitrpm.version ] ; then
		echo "$OUTDIR/$REPONAME-gitrpm.version has not been created yet!" >&2
		echo "Please run gitrpm-version.sh w/o -B to generate it first." >&2
		exit 2
	fi

	RESULT=$(grep "^$BOOTSTRAP: " "$OUTDIR"/$REPONAME-gitrpm.version | cut -f 2 -d ' ')
	if [ "$DO_ZEROEXTRA" == 'y' ] ; then
	    if [ -n "$RESULT" ] ; then
		echo "0.$RESULT."
	    fi
	else
	    echo "$RESULT"
	fi
	
fi


#eof
