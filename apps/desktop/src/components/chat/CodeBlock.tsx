import React, { useState } from "react";
import chatStyles from "../../styles/chat.module.css";

const CodeBlock = React.memo(function CodeBlock({ children, className, ...props }: any) {
  const [copied, setCopied] = useState(false);
  const match = /language-(\w+)/.exec(className || "");
  const lang = match ? match[1] : "";
  const code = String(children).replace(/\n$/, "");

  const handleCopy = () => {
    navigator.clipboard.writeText(code);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className={chatStyles.codeBlock}>
      <div className={chatStyles.codeBlockHeader}>
        <span className={chatStyles.codeBlockLang}>{lang || "code"}</span>
        <button className={chatStyles.codeBlockCopy} onClick={handleCopy}>
          {copied ? "✓ Copied" : "Copy"}
        </button>
      </div>
      <div className={chatStyles.codeBlockBody}>
        <pre><code className={className} {...props}>{children}</code></pre>
      </div>
    </div>
  );
});

export { CodeBlock };
