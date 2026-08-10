import { StrictMode, Component } from "react";
import type { ReactNode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./styles/pro.css";
import "./styles/lite.css";

// 渲染异常兜底：与其白屏，不如把错误摆出来（这次白屏定位就是它抓的）
class DebugBoundary extends Component<
  { children: ReactNode },
  { error: Error | null }
> {
  state = { error: null as Error | null };
  static getDerivedStateFromError(error: Error) {
    return { error };
  }
  render() {
    if (this.state.error) {
      return (
        <pre style={{ padding: 24, color: "red", whiteSpace: "pre-wrap" }}>
          {String(this.state.error)}
          {"\n"}
          {this.state.error.stack}
        </pre>
      );
    }
    return this.props.children;
  }
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <DebugBoundary>
      <App />
    </DebugBoundary>
  </StrictMode>,
);
