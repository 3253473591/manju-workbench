import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App } from "./App";
import "./styles/base.css";
import "./styles/app.css";
import "./styles/workspace.css";

// 写操作一律 invalidate 而不是手改缓存（`02` §8）—— 所以默认 staleTime 设 0
const client = new QueryClient({
  defaultOptions: { queries: { staleTime: 0, refetchOnWindowFocus: true, retry: false } },
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={client}>
      <App />
    </QueryClientProvider>
  </React.StrictMode>,
);
