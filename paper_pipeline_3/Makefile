.PHONY: all pdf pdf-double-blind artifact artifact-double-blind clean

MD_SINGLE := Content-Keyed-CRDTs.md
PDF_SINGLE := Content-Keyed-CRDTs.pdf

MD_DOUBLE := paper_double_blind.md
PDF_DOUBLE := paper_double_blind.pdf

ARTIFACT_SINGLE_ZIP := paper_pipeline_3_artifact_single_blind.zip
ARTIFACT_DOUBLE_ZIP := paper_pipeline_3_artifact_double_blind.zip

all: pdf pdf-double-blind artifact artifact-double-blind

pdf:
	pandoc $(MD_SINGLE) \
		--pdf-engine=xelatex \
		-V geometry:margin=1in \
		-V fontsize=11pt \
		-V colorlinks=true \
		-o $(PDF_SINGLE)

pdf-double-blind:
	pandoc $(MD_DOUBLE) \
		--pdf-engine=xelatex \
		-V geometry:margin=1in \
		-V fontsize=11pt \
		-V colorlinks=true \
		-o $(PDF_DOUBLE)

artifact:
	zip -r $(ARTIFACT_SINGLE_ZIP) $(MD_SINGLE) $(PDF_SINGLE) benchmark.py Makefile README.md

artifact-double-blind:
	zip -r $(ARTIFACT_DOUBLE_ZIP) $(MD_DOUBLE) $(PDF_DOUBLE) benchmark.py Makefile README.md

clean:
	rm -f $(PDF_SINGLE) $(PDF_DOUBLE) $(ARTIFACT_SINGLE_ZIP) $(ARTIFACT_DOUBLE_ZIP)
