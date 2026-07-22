.PHONY: all pdf pdf-double-blind artifact clean

MD_SINGLE := "Conflict-Free Knowledge Graph Projection.md"
PDF_SINGLE := "Conflict-Free Knowledge Graph Projection.pdf"

MD_DOUBLE := "paper_double_blind.md"
PDF_DOUBLE := "paper_double_blind.pdf"

ARTIFACT_ZIP := "paper_pipeline_artifact.zip"

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
	zip -r $(ARTIFACT_ZIP) crdt_projection.py test_pipeline.py test_adversarial.py benchmark.py README.md CITATION.cff

clean:
	rm -f $(PDF_SINGLE) $(PDF_DOUBLE) $(ARTIFACT_ZIP)
