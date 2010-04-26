#!/bin/bash



abs_path() {
	if echo "$1" | grep '^/' > /dev/null ; then
		echo "$1"
	else
		echo $(pwd)/"$1"
	fi
}

usage() {
	cat '-' <<EOF
Usage: $0 [-q] [-f] [-p PREFIX] HEAD <outfile.tar>

EOF
}

SPREFIX=
while test $# -ne 0
do
	case "$1" in
	-q|--quiet)
		quiet=1
		;;
	-f|--force)
		force=1
		;;
	-p)
		SPREFIX="$2"
		shift 1
		;;
	--)
		shift
		break
		;;
	-*)
		usage
		;;
	*)
		break
		;;
	esac
	shift
done

GHEAD=$1
OFILE=$2

if [ -z "$2" ] ; then
	usage
	exit 1
fi

ABS_OFILE=$(abs_path "$OFILE")

export SPREFIX
export ABS_OFILE
git archive --format=tar --prefix="$SPREFIX" $GHEAD > $OFILE

git submodule foreach "TMPTAR=\$(mktemp --tmpdir archive.tar.XXXXXX) ;" \
	"(git archive --format=tar --prefix=$SPREFIX\$path/ \$sha1 > \$TMPTAR ) && " \
	"tar -Af $ABS_OFILE \$TMPTAR && rm -f \$TMPTAR "


#eof

