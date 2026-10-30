# Build with tectonic (runs BibTeX and re-runs LaTeX as needed). Output: thesis.pdf
default: thesis.pdf

thesis.pdf: thesis.tex references.bib $(wildcard content/*.tex) $(wildcard latex/*.tex)
	tectonic thesis.tex

clean:
	rm -f thesis.pdf thesis.aux thesis.bbl thesis.blg thesis.log thesis.lof thesis.lot thesis.toc thesis.out
