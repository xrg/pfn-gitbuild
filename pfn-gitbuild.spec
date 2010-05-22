%define git_repo gitscripts
%define git_head HEAD

%define name pfn-gitbuild
%define version %git_get_ver
%define release %mkrel %git_get_rel

# this will force "/usr/lib/" even on 64-bit
%define libndir %{_exec_prefix}/lib

Name:		%{name}
Version:	%{version}
Release:	%{release}
Summary:	Pefnos Git Build scripts
Group:		Development/Other
BuildArch:	noarch
License:	GPL
Source0:	%git_bs_source %{name}-%{version}.tar.gz
Source1:	%{name}-gitrpm.version
Source2:	%{name}-changelog.gitrpm.txt

#BuildRequires:	gettext
Requires(pre): rpm-build
%if %{_target_vendor} == redhat
BuildRoot:	%{_tmppath}/%{name}-%{version}-%{release}-buildroot
%endif

%description
Pfn-gitbuild is a set of scripts that enables direct building of rpms
from a git repository.
Install it and port your .spec files to build from the devel repos. If
the repo is not there, the macros will still be able to build from the
SRPM file created.


%prep
%git_get_source
%setup -q


%build
# nothing to build!

%install
[ -n "%{buildroot}" -a "%{buildroot}" != / ] && rm -rf %{buildroot}

install -d %{buildroot}%{_bindir} \
	%{buildroot}%{_sys_macros_dir}
	
install bin/gitrpm-changelog bin/gitrpm-version.sh bin/git-init-pomerge  \
	%{buildroot}%{_bindir}/
install lib/rpmmacros/* %{buildroot}%{_sys_macros_dir}



%clean
[ -n "%{buildroot}" -a "%{buildroot}" != / ] && rm -rf %{buildroot}


%files
%defattr(-,root,root)
                  %{_sys_macros_dir}/*
%attr(0755,root,backup)  %{_bindir}/*

%changelog -f %{_sourcedir}/%{name}-changelog.gitrpm.txt

