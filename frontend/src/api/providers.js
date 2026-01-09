import axios from "axios";

const API_URL = "http://localhost:8000/proveedores";

export const getProviders = async () => {
  const res = await axios.get(API_URL);
  return res.data;
};

export const createProvider = async (data) => {
  const res = await axios.post(API_URL, data);
  return res.data;
};

export const updateProvider = async (id, data) => {
  const res = await axios.put(`${API_URL}/${id}`, data);
  return res.data;
};

export const deleteProvider = async (id) => {
  await axios.delete(`${API_URL}/${id}`);
};
