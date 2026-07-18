#!/usr/bin/env bash
# Downloads the reference library (free books, official docs, interview
# questions) into ./library — this folder is gitignored because some sources
# (Beej) allow personal use but not redistribution.
# After it finishes: python rag.py build
set -e
cd "$(dirname "$0")"
mkdir -p library/c library/cpp library/python library/interview
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

echo "Beej's Guide to C (single-page HTML)..."
curl -s https://beej.us/guide/bgc/html/ -o library/c/beej_guide_to_c.html

echo "Beej's C Library Reference (stdlib functions, single-page HTML)..."
curl -s https://beej.us/guide/bgclr/html/ -o library/c/beej_c_library_reference.html \
  || echo "  [skip: bgclr unavailable]"

echo "GNU C Intro & Reference manual (full C language, GFDL)..."
curl -s https://www.gnu.org/software/c-intro-and-ref/manual/c-intro-and-ref.html \
  -o library/c/gnu_c_intro_and_ref.html || echo "  [skip: GNU manual unavailable]"

echo "Ted Jensen's Tutorial on Pointers and Arrays..."
git clone --depth 1 --quiet https://github.com/jflaherty/ptrtut13.git "$tmp/ptrtut" \
  && mkdir -p library/c/pointer-tutorial \
  && find "$tmp/ptrtut" \( -name "*.md" -o -name "*.txt" -o -name "*.html" \) \
       -not -path "*/.git/*" -exec cp {} library/c/pointer-tutorial/ \; \
  || echo "  [skip: ptrtut13 unavailable]"

echo "Wikibooks: C Programming (full book, single page, CC-BY-SA)..."
curl -s "https://en.wikibooks.org/wiki/C_Programming/Print_version" \
  -o library/c/wikibooks_c_programming.html || echo "  [skip: wikibooks unavailable]"

echo "C++ Core Guidelines..."
curl -s https://raw.githubusercontent.com/isocpp/CppCoreGuidelines/master/CppCoreGuidelines.md \
  -o library/cpp/cpp_core_guidelines.md

echo "Modern C++ Tutorial (book, English)..."
git clone --depth 1 --quiet https://github.com/changkun/modern-cpp-tutorial.git "$tmp/mct"
mkdir -p library/cpp/modern-cpp-tutorial
cp "$tmp/mct"/book/en-us/*.md library/cpp/modern-cpp-tutorial/

echo "Python official docs (tutorial, faq, howto)..."
curl -s https://docs.python.org/3/archives/python-3.14-docs-text.zip -o "$tmp/pydocs.zip"
unzip -q -o "$tmp/pydocs.zip" -d "$tmp/pydocs"
cp -r "$tmp/pydocs"/*/tutorial "$tmp/pydocs"/*/faq "$tmp/pydocs"/*/howto library/python/

echo "Interview question collections..."
for r in Devinterview-io/cpp-interview-questions \
         Devinterview-io/python-interview-questions \
         vishnumotghare/Embedded-Systems-and-Linux-Interview-Questions; do
  name=$(basename "$r")
  git clone --depth 1 --quiet "https://github.com/$r.git" "$tmp/$name"
  mkdir -p "library/interview/$name"
  find "$tmp/$name" -name "*.md" -not -path "*/.git/*" -exec cp {} "library/interview/$name/" \;
done
git clone --depth 1 --quiet https://github.com/learning-zone/python-interview-questions.git "$tmp/lz-python"
mkdir -p library/interview/python-interview-500
find "$tmp/lz-python" -name "*.md" -not -path "*/.git/*" -exec cp {} library/interview/python-interview-500/ \;

echo "Done. Now run: python rag.py build"
