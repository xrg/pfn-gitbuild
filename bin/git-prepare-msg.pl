#!/usr/bin/perl

# Prepare git commit message, taking submodules into account.
# Copyright (C) 2009, P. Christeas <p_christ@hol.gr>

use locale;

my $commfile = shift(@ARGV);
#print ("Git prepare msg called with: $commfile\n\n");

my $origfile = $commfile .".orig";
rename($commfile,$origfile) or die ("Cannot rename to $origfile");
open(infile,"<",$origfile) or die("Cannot open $origfile");
open(outfile,">",$commfile) or die("Cannot open $commfile");

my $lastline = "";
while (<infile>){
	if ( ! /^\#/) {
		print outfile $_ ;
		next;
	}
	$lastline = $_;
	last;
}


my @modsChanged;
if (open(din,"-|","git diff --cached" ) ){
	while(<din>){
		chomp;
		if (! /^diff /) { next ; }
		$_ = <din> ;
		
		# Only consider submodule (by attrs) changes
		if (! /^index [^ ]* 16000/) { next ; }
		$_ = <din>;
		if (! /^--- a\// ) {
			next;
		}
		$_ = <din>;
		if (! /^\+\+\+ b\/(.*)$/ ){
			next;
		}
		my $submod = $1;
		push(@modsChanged,$submod);
	}
	print outfile "Updated submodules ".join(", ",@modsChanged);
	print outfile "\n\n";
	close(din);
}else {
 warn("Cannot open git diff: $!");
}

if (open(din,"-|","git diff --cached" ) ){
	print outfile "# Detailed diff of submodules:\n";

	while(<din>){
		chomp;
		if (! /^diff /) { next ; }
		$_ = <din> ;
		
		# Only consider submodule (by attrs) changes
		if (! /^index [^ ]* 16000/) { next ; }
		$_ = <din>;
		if (! /^--- a\// ) {
			warn("Garbled diff out: $_");
			next;
		}
		$_ = <din>;
		if (! /^\+\+\+ b\/(.*)$/ ){
			warn("Garbled diff in: $_");
			next;
		}
		my $submod = $1;
		push(@modsChanged,$submod);
		
		$_ = <din>;
		$_ = <din>;
		chomp;
		if (! /^-Subproject commit (.*)$/) {
			warn("Garbled diff out: $_");
			next;
		}
		my $oldhash = $1;
		$_ = <din>;
		chomp;
		if (! /^\+Subproject commit (.*)$/) {
			warn("Garbled diff in: $_");
			next;
		}
		my $newhash = $1;
		
		print outfile "Submodule $submod:\n";
		
		if (open(spin,"-|","cd $submod ; git shortlog -nes $oldhash..$newhash" ) ){
			my $lastline = "";
			while (<spin>){
				if ( /^ *\[MERGE\] *$/i ) {
				    print "skip $_";
				    next;
				}
				if ( /^ *merge *$/i ) {
				    print "Skip $_";
				    next;
				}
				if ( $_ eq $lastline ) {
				    next ;
				}
				$lastline = $_;
				print outfile "    ".$_;
			}
		}
		else{
			warn ("Cannot git-log: $!");
		}

	}
	close(din);
}else {
 warn("Cannot open git diff: $!");
}

# Output the rest infile lines
do {
	print outfile $lastline ;
	$lastline=$_;
} while (<infile>);


close(infile);
close(outfile);
unlink($origfile);
exit( 0);

#eof

