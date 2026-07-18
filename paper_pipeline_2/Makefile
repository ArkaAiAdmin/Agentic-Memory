.PHONY: pdf clean

MD := "Conflict-Free Knowledge Graph Projection - Content-Keyed CRDT Framework.md"
PDF := "Conflict-Free Knowledge Graph Projection - Content-Keyed CRDT Framework.pdf"

pdf:
	pandoc $(MD) \
		--pdf-engine=xelatex \
		-V geometry:margin=1in \
		-V fontsize=11pt \
		-V colorlinks=true \
		-o $(PDF)

clean:
	rm -f $(PDF)
