import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { Pipeline } from "./pages/Pipeline";
import { Quality } from "./pages/Quality";
import { Review } from "./pages/Review";
import { Schemas } from "./pages/Schemas";

export const routes = (
  <Routes>
    <Route element={<Layout />}>
      <Route index element={<Pipeline />} />
      <Route path="qualite" element={<Quality />} />
      <Route path="schemas" element={<Schemas />} />
      <Route path="revue" element={<Review />} />
      <Route path="*" element={<Pipeline />} />
    </Route>
  </Routes>
);

export const makeClient = (): QueryClient => new QueryClient({ defaultOptions: { queries: { refetchOnWindowFocus: false, staleTime: 15_000 } } });

export function App() {
  return (
    <QueryClientProvider client={makeClient()}>
      <BrowserRouter>{routes}</BrowserRouter>
    </QueryClientProvider>
  );
}
