.PHONY: all pdf pdf-double-blind artifact clean

MD_SINGLE := "Conflict-Free Knowledge Graph Projection - Content-Keyed CRDT Framework.md"
PDF_SINGLE := "Conflict-Free Knowledge Graph Projection - Content-Keyed CRDT Framework.pdf"

MD_DOUBLE := "paper_double_blind.md"
PDF_DOUBLE := "paper_double_blind.pdf"

ARTIFACT_ZIP := "paper_pipeline_2_artifact.zip"

all: pdf pdf-double-blind artifact

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
	zip -r $(ARTIFACT_ZIP) ck_crdt.py test_adversarial.py CITATION.cff

clean:
	rm -f $(PDF_SINGLE) $(PDF_DOUBLE) $(ARTIFACT_ZIP)
