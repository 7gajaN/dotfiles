#
# ~/.bashrc
#

export CC=gcc-8
export CXX=g++-8
# export COMPILER=gcc8
# export COMPILER_EXT=gcc8
# export CFLAGS="-O2"
# export CXXFLAGS="-O2"
# export LDFLAGS="-lpthread"
# export LD_LIBRARY_PATH=/usr/lib/gcc/x86_64-pc-linux-gnu/8.5.0:$LD_LIBRARY_PATH
# export LD_LIBRARY_PATH=/home/gion/coding/misc/piper-tts/piper1-gpl/libpiper/install:/home/gion/coding/misc/piper-tts/piper1-gpl/libpiper/install/lib:$LD_LIBRARY_PATH

# Setting the java to 8 for the mediaserver project
# Set JAVA_HOME to OpenJDK 8
export JAVA_HOME=/usr/lib/jvm/java-8-openjdk

# Prepend JAVA_HOME/bin to PATH so 'java' uses version 8
export PATH="$JAVA_HOME/bin:$PATH"

echo "JAVA_HOME is set to $JAVA_HOME"
java -version

echo "GCC version is set to $CC"
gcc-8 --version

echo "G++ version is set to $CXX"
g++-8 --version

# If not running interactively, don't do anything
[[ $- != *i* ]] && return

alias ls='ls --color=auto'
alias grep='grep --color=auto'
PS1='[\u@\h \W]\$ '

xrdb -merge ~/.Xresources
